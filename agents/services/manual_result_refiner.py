"""Rewrite a Manual run tester's notes for TestRail (wording only).

One tool-free LLM call. The input (case title, verbatim steps with the
tester's outcome and note, the case note) is fenced as data; the answer is
strict JSON with the rewritten case note and step notes. Code validates the
shape and the lengths and keeps the tester's own note wherever a rewrite
dropped one of its literal values (numbers, IDs, paths, quoted text). The
model never sees or returns a status: outcomes stay the caller's. This module
knows nothing of the ORM.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final, Literal

from agents.exceptions import AIServiceError, AIServiceErrorKind
from agents.services.llm_client import send_chat_completion
from agents.services.manual_result_refiner_prompts import (
    MANUAL_REFINE_PROMPT_VERSION,
    MANUAL_REFINE_SYSTEM_PROMPT,
    build_manual_refine_user_prompt,
)
from agents.types import ChatMessage, LLMConfig

ManualStepOutcome = Literal["passed", "failed", "not_run"]

# A rewrite may be at most this many times its input (or this floor).
_LENGTH_FACTOR: Final = 3
_LENGTH_FLOOR: Final = 400
# Context only: step texts are capped in the prompt (notes are never cut).
_STEP_TEXT_PROMPT_MAX_CHARS: Final = 1000
_TITLE_PROMPT_MAX_CHARS: Final = 500

# Literal values a rewrite must keep verbatim: quoted spans, and tokens that
# hold a digit or look like a path or URL.
_QUOTED: Final = re.compile(r"\"[^\"\n]+\"|'[^'\n]+'|`[^`\n]+`|“[^”\n]+”|‘[^’\n]+’")
_TOKEN: Final = re.compile(r"\S*(?:\d|://|[/\\])\S*")
_TRAILING_PUNCTUATION: Final = ".,;:!?)]}"
_LEADING_PUNCTUATION: Final = "([{"


@dataclass(frozen=True)
class ManualRefineStep:
    """One verbatim case step with the tester's outcome and note."""

    num: int
    text: str
    expected: str
    outcome: ManualStepOutcome
    note: str


@dataclass(frozen=True)
class ManualRefineInput:
    case_title: str
    # The case status label ("Passed", "Failed", "Blocked", ...); context only.
    case_outcome: str
    steps: tuple[ManualRefineStep, ...]
    case_note: str

    @property
    def has_notes(self) -> bool:
        return bool(self.case_note.strip()) or any(
            step.note.strip() for step in self.steps
        )


@dataclass(frozen=True)
class ManualRefineOutput:
    """The rewritten case note ("" when the input had none) and step notes
    (exactly the steps whose input note was not empty)."""

    case_note: str
    step_notes: dict[int, str]


def manual_refine_input_hash(data: ManualRefineInput) -> str:
    """SHA-256 of everything the rewrite depends on, prompt version included."""
    canonical = json.dumps(
        {"version": MANUAL_REFINE_PROMPT_VERSION, "input": _payload(data, cap=False)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def refine_manual_result(
    llm: LLMConfig, data: ManualRefineInput, *, timeout_seconds: float
) -> ManualRefineOutput:
    """The rewritten notes; raises ``AIServiceError`` on any unusable answer.

    Call only when ``data.has_notes``. A rewritten note that lost one of its
    literal values is replaced by the tester's own note.
    """
    messages = (
        ChatMessage(role="system", content=MANUAL_REFINE_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=build_manual_refine_user_prompt(_payload(data, cap=True)),
        ),
    )
    response = send_chat_completion(
        llm, messages, (), timeout_seconds=max(1.0, timeout_seconds)
    )
    content = response.message.content
    if not isinstance(content, str) or not content.strip():
        raise _bad("The AI service returned an empty rewrite.")
    return _validate(data, _parse_json(content))


def _payload(data: ManualRefineInput, *, cap: bool) -> dict[str, object]:
    def text(value: str, limit: int) -> str:
        return value[:limit] if cap else value

    return {
        "case_title": text(data.case_title, _TITLE_PROMPT_MAX_CHARS),
        "case_outcome": data.case_outcome,
        "steps": [
            {
                "num": step.num,
                "text": text(step.text, _STEP_TEXT_PROMPT_MAX_CHARS),
                "expected": text(step.expected, _STEP_TEXT_PROMPT_MAX_CHARS),
                "outcome": step.outcome,
                "note": step.note,
            }
            for step in data.steps
        ],
        "case_note": data.case_note,
    }


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
        raise _bad("The AI service returned a rewrite that is not JSON.") from exc
    if not isinstance(parsed, dict):
        raise _bad("The AI service returned a rewrite that is not a JSON object.")
    return {str(key): value for key, value in parsed.items()}


def _validate(data: ManualRefineInput, parsed: dict[str, object]) -> ManualRefineOutput:
    raw_case_note = parsed.get("case_note", "")
    raw_step_notes = parsed.get("step_notes", {})
    if not isinstance(raw_case_note, str) or not isinstance(raw_step_notes, dict):
        raise _bad("The AI service returned a rewrite with the wrong shape.")
    inputs = {step.num: step.note for step in data.steps if step.note.strip()}
    step_notes: dict[int, str] = {}
    for key, value in raw_step_notes.items():
        num = int(key) if isinstance(key, str) and key.isdigit() else None
        if num is None or num not in inputs or not isinstance(value, str):
            raise _bad("The AI service rewrote a step note that does not exist.")
        step_notes[num] = _checked(inputs[num], value)
    if set(step_notes) != set(inputs):
        raise _bad("The AI service left out a step note.")
    case_note = (
        _checked(data.case_note, raw_case_note) if data.case_note.strip() else ""
    )
    return ManualRefineOutput(case_note=case_note, step_notes=step_notes)


def _checked(original: str, rewritten: str) -> str:
    """The rewrite after the length and literal checks."""
    text = rewritten.strip()
    if not text:
        raise _bad("The AI service returned an empty note.")
    if len(text) > max(_LENGTH_FACTOR * len(original), _LENGTH_FLOOR):
        raise _bad("The AI service returned a note far longer than the original.")
    if any(literal not in text for literal in _literals(original)):
        return original.strip()
    return text


def _literals(text: str) -> set[str]:
    literals = {match.group(0) for match in _QUOTED.finditer(text)}
    for match in _TOKEN.finditer(text):
        token = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        token = token.lstrip(_LEADING_PUNCTUATION)
        if token:
            literals.add(token)
    return literals


def _bad(message: str) -> AIServiceError:
    return AIServiceError(AIServiceErrorKind.BAD_RESPONSE, message)
