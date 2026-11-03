"""Classify the cases of a test run: category, severity and a one-line finding.

One tool-free LLM call per batch. Each case is fenced as data under its opaque
ref, its screenshots attached as labelled image parts; the answer is strict
JSON. Code checks that every input ref comes back exactly once with taxonomy
values and a non-empty finding, and raises ``AIServiceError`` otherwise so the
caller can retry in halves. This module knows nothing of the ORM.
"""

from __future__ import annotations

import json
import re
from typing import Final

from agents.exceptions import AIServiceError, AIServiceErrorKind
from agents.services.llm_client import send_chat_completion
from agents.services.run_case_classifier_prompts import (
    CLASSIFIER_INTRO,
    build_case_block,
    build_classifier_system_prompt,
    build_context_block,
    screenshot_label,
)
from agents.triage_types import (
    CaseClassification,
    TriageCaseInput,
    TriageTaxonomyText,
)
from agents.types import ChatMessage, ContentPart, ImageContent, LLMConfig, TextContent

FINDING_MAX_CHARS: Final = 300
_WHITESPACE: Final = re.compile(r"\s+")


def classify_run_cases(
    llm: LLMConfig,
    cases: tuple[TriageCaseInput, ...],
    *,
    application_context: str,
    taxonomy: TriageTaxonomyText,
    timeout_seconds: float,
) -> tuple[CaseClassification, ...]:
    """One classification per input case, in input order.

    Raises ``AIServiceError`` on any unusable answer (not JSON, a ref missing,
    duplicated or unknown, a value outside the taxonomy, an empty finding).
    """
    if not cases:
        return ()
    messages = (
        ChatMessage(role="system", content=build_classifier_system_prompt(taxonomy)),
        ChatMessage(role="user", content=_user_parts(cases, application_context)),
    )
    response = send_chat_completion(
        llm, messages, (), timeout_seconds=max(1.0, timeout_seconds)
    )
    content = response.message.content
    if not isinstance(content, str) or not content.strip():
        raise bad_answer("The AI service returned an empty classification.")
    return _validate(cases, taxonomy, parse_json_object(content))


def _user_parts(
    cases: tuple[TriageCaseInput, ...], application_context: str
) -> tuple[ContentPart, ...]:
    parts: list[ContentPart] = [
        TextContent(text=CLASSIFIER_INTRO),
        TextContent(text=build_context_block(application_context)),
    ]
    for case in cases:
        parts.append(TextContent(text=build_case_block(_payload(case))))
        for shot in case.screenshots:
            parts.append(TextContent(text=screenshot_label(case.ref, shot.step_num)))
            parts.append(
                ImageContent(base64_data=shot.base64_data, media_type=shot.media_type)
            )
    return tuple(parts)


def _payload(case: TriageCaseInput) -> dict[str, object]:
    return {
        "ref": case.ref,
        "title": case.title,
        "status": case.status_label,
        "case_text": case.case_text,
        "result_text": case.result_text,
        "log_tail": case.log_tail,
    }


def _validate(
    cases: tuple[TriageCaseInput, ...],
    taxonomy: TriageTaxonomyText,
    parsed: dict[str, object],
) -> tuple[CaseClassification, ...]:
    raw_cases = parsed.get("cases")
    if not isinstance(raw_cases, list):
        raise bad_answer("The AI service returned a classification without cases.")
    categories = {value for value, _label, _meaning in taxonomy.categories}
    severities = {value for value, _label, _meaning in taxonomy.severities}
    wanted = {case.ref for case in cases}
    found: dict[str, CaseClassification] = {}
    for item in raw_cases:
        if not isinstance(item, dict):
            raise bad_answer("The AI service returned a case that is not an object.")
        ref = item.get("ref")
        if not isinstance(ref, str) or ref not in wanted:
            raise bad_answer("The AI service classified a case it was not given.")
        if ref in found:
            raise bad_answer("The AI service classified a case twice.")
        category = _enum(item.get("category"), categories)
        severity = _enum(item.get("severity"), severities)
        if category is None or severity is None:
            raise bad_answer("The AI service returned a value outside the taxonomy.")
        finding = _finding(item.get("finding"))
        if not finding:
            raise bad_answer("The AI service returned an empty finding.")
        found[ref] = CaseClassification(
            ref=ref, category=category, severity=severity, finding=finding
        )
    if len(found) != len(wanted):
        raise bad_answer("The AI service left out a case.")
    return tuple(found[case.ref] for case in cases)


def _enum(value: object, allowed: set[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in allowed else None


def _finding(value: object) -> str:
    return one_line(value, FINDING_MAX_CHARS) if isinstance(value, str) else ""


def one_line(text: str, limit: int) -> str:
    """``text`` collapsed to one line and cut to ``limit`` characters."""
    collapsed = _WHITESPACE.sub(" ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def parse_json_object(content: str) -> dict[str, object]:
    """The answer as a JSON object; ``AIServiceError`` when it is not one."""
    text = content.strip()
    if text.startswith("```"):
        # Tolerate a fenced answer: drop the first and the last fence line.
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        parsed: object = json.loads(text)
    except ValueError as exc:
        raise bad_answer("The AI service returned an answer that is not JSON.") from exc
    if not isinstance(parsed, dict):
        raise bad_answer("The AI service returned an answer that is not an object.")
    return {str(key): value for key, value in parsed.items()}


def bad_answer(message: str) -> AIServiceError:
    return AIServiceError(AIServiceErrorKind.BAD_RESPONSE, message)
