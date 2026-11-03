"""Judge which screenshots of a checked case to attach to its TestRail result.

One tool-free LLM call. The text input (case title, verdict, the causing
steps with a bounded log excerpt, or the manual steps with the tester's marks
and notes) is fenced as data; every candidate image follows it, each after
its opaque label. The answer is strict JSON with one judgment per label. Code
validates that every label appears exactly once with a real boolean; the
per-step limit is applied again by the caller. The model never sees or
returns a verdict change. This module knows nothing of the ORM.
"""

from __future__ import annotations

import base64
import json
import unicodedata
from dataclasses import dataclass
from typing import Final

from agents.exceptions import AIServiceError, AIServiceErrorKind
from agents.services.evidence_review_prompts import (
    EVIDENCE_REVIEW_PROMPT_VERSION,
    EVIDENCE_REVIEW_SYSTEM_PROMPT,
    build_evidence_review_user_prompt,
    screenshot_heading,
)
from agents.services.llm_client import send_chat_completion
from agents.types import ChatMessage, ContentPart, ImageContent, LLMConfig, TextContent

__all__ = [
    "EVIDENCE_REVIEW_PROMPT_VERSION",
    "EvidenceImage",
    "EvidenceJudgment",
    "EvidenceManualStep",
    "EvidenceReviewInput",
    "EvidenceStepContext",
    "review_evidence",
]

_REASON_MAX_CHARS: Final = 200
# Context only: texts are capped in the prompt (the caller bounds the rest).
_TITLE_PROMPT_MAX_CHARS: Final = 500
_STEP_TEXT_PROMPT_MAX_CHARS: Final = 1000


@dataclass(frozen=True)
class EvidenceImage:
    """One candidate screenshot, already downscaled."""

    label: str  # opaque "S1".."Sn"
    step_num: int | None
    kind: str  # tool name or manual shot kind, e.g. "take_screenshot"
    jpeg: bytes


@dataclass(frozen=True)
class EvidenceStepContext:
    """An agent case step that caused the verdict."""

    step_num: int
    description: str
    failure_summary: str


@dataclass(frozen=True)
class EvidenceManualStep:
    """A verbatim manual case step with the tester's mark and note."""

    step_num: int
    text: str
    mark: str  # "pass" | "fail" | "" (not marked)
    note: str


@dataclass(frozen=True)
class EvidenceReviewInput:
    case_title: str
    verdict_label: str  # "Passed" | "Failed" | "Blocked"
    verdict_text: str  # already truncated by the caller
    manual: bool
    steps: tuple[EvidenceStepContext, ...]
    log_excerpt: str  # agent: bounded excerpt of the causing step; "" for manual
    manual_steps: tuple[EvidenceManualStep, ...]
    manual_case_note: str
    images: tuple[EvidenceImage, ...]
    per_step: int  # at most this many relevant shots per step


@dataclass(frozen=True)
class EvidenceJudgment:
    relevant: bool
    reason: str  # <= 200 chars


def review_evidence(
    llm: LLMConfig, data: EvidenceReviewInput, *, timeout_seconds: float
) -> dict[str, EvidenceJudgment]:
    """label -> judgment for every image; raises AIServiceError on any unusable answer."""
    if not data.images:
        return {}
    parts: list[ContentPart] = [
        TextContent(text=build_evidence_review_user_prompt(_payload(data)))
    ]
    for image in data.images:
        parts.append(TextContent(text=screenshot_heading(image.label)))
        parts.append(
            ImageContent(
                base64_data=base64.b64encode(image.jpeg).decode("ascii"),
                media_type="image/jpeg",
            )
        )
    messages = (
        ChatMessage(role="system", content=EVIDENCE_REVIEW_SYSTEM_PROMPT),
        ChatMessage(role="user", content=tuple(parts)),
    )
    response = send_chat_completion(
        llm, messages, (), timeout_seconds=max(1.0, timeout_seconds)
    )
    content = response.message.content
    if not isinstance(content, str) or not content.strip():
        raise _bad("The AI service returned an empty screenshot review.")
    return _validate(data, _parse_json(content))


def _payload(data: EvidenceReviewInput) -> dict[str, object]:
    payload: dict[str, object] = {
        "case_title": data.case_title[:_TITLE_PROMPT_MAX_CHARS],
        "verdict": data.verdict_label,
        "verdict_text": data.verdict_text,
        "manual": data.manual,
    }
    if data.manual:
        payload["manual_steps"] = [
            {
                "step_num": step.step_num,
                "text": step.text[:_STEP_TEXT_PROMPT_MAX_CHARS],
                "mark": step.mark,
                "note": step.note,
            }
            for step in data.manual_steps
        ]
        payload["case_note"] = data.manual_case_note
    else:
        payload["steps"] = [
            {
                "step_num": step.step_num,
                "description": step.description[:_STEP_TEXT_PROMPT_MAX_CHARS],
                "failure_summary": step.failure_summary[:_STEP_TEXT_PROMPT_MAX_CHARS],
            }
            for step in data.steps
        ]
        payload["log_excerpt"] = data.log_excerpt
    payload["screenshots"] = [
        {"label": image.label, "step_num": image.step_num, "kind": image.kind}
        for image in data.images
    ]
    payload["max_relevant_per_step"] = max(0, data.per_step)
    return payload


def _parse_json(content: str) -> dict[str, object]:
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
        raise _bad(
            "The AI service returned a screenshot review that is not JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise _bad(
            "The AI service returned a screenshot review that is not a JSON object."
        )
    return {str(key): value for key, value in parsed.items()}


def _validate(
    data: EvidenceReviewInput, parsed: dict[str, object]
) -> dict[str, EvidenceJudgment]:
    shots = parsed.get("shots")
    if not isinstance(shots, list):
        raise _bad("The AI service returned a screenshot review with the wrong shape.")
    labels = {image.label for image in data.images}
    judgments: dict[str, EvidenceJudgment] = {}
    for shot in shots:
        if not isinstance(shot, dict):
            raise _bad(
                "The AI service returned a screenshot review with the wrong shape."
            )
        label = shot.get("label")
        relevant = shot.get("relevant")
        reason = shot.get("reason", "")
        if not isinstance(label, str) or label not in labels:
            raise _bad("The AI service judged a screenshot that does not exist.")
        if label in judgments:
            raise _bad("The AI service judged a screenshot twice.")
        if not isinstance(relevant, bool):
            raise _bad("The AI service returned a judgment that is not a boolean.")
        if not isinstance(reason, str):
            raise _bad("The AI service returned a reason that is not text.")
        judgments[label] = EvidenceJudgment(relevant=relevant, reason=_clean(reason))
    if set(judgments) != labels:
        raise _bad("The AI service left out a screenshot.")
    return judgments


def _clean(reason: str) -> str:
    """The reason without control characters, stripped and capped."""
    text = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in reason
    )
    return " ".join(text.split())[:_REASON_MAX_CHARS]


def _bad(message: str) -> AIServiceError:
    return AIServiceError(AIServiceErrorKind.BAD_RESPONSE, message)
