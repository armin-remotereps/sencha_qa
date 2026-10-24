from __future__ import annotations

import logging
import re

from agents.services.llm_client import send_chat_completion
from agents.types import (
    ChatMessage,
    ImageContent,
    LLMConfig,
    OutcomeStatus,
    OutcomeVerdict,
    PixelParseResult,
    PixelUIElement,
    TextContent,
    Verdict,
)

logger = logging.getLogger(__name__)

_CANDIDATE_SYSTEM_PROMPT = (
    "You are a careful UI verification assistant. You will be shown an "
    "annotated screenshot with a numbered bounding box around one candidate "
    "UI element, and the description of the element the agent is looking "
    "for.\n\n"
    "Decide whether the numbered box truly is that element.\n"
    "Reply with YES or NO on the first line, then one short reason on the "
    "next line."
)

_OUTCOME_SYSTEM_PROMPT = (
    "You are a careful UI verification assistant. You will be shown a "
    "BEFORE and an AFTER screenshot of a desktop, taken immediately before "
    "and after the agent performed an action, along with a summary of the "
    "action and the result the agent expected.\n\n"
    "Answer on the first line with exactly one word:\n"
    "YES — the expected result is visible in the AFTER screenshot.\n"
    "CHANGED — the intended element was acted on and the screen changed in "
    "response, but the expected result is not shown, or only partly (for "
    "example the right page opened but a detail the agent expected is "
    "elsewhere or missing).\n"
    "NO — nothing visibly changed, or a different element or the wrong "
    "place was acted on.\n"
    "Then one short reason on the next line."
)

_OUTCOME_WORDS: dict[str, OutcomeStatus] = {
    "YES": "confirmed",
    "CHANGED": "unexpected",
    "NO": "missed",
}
# Leading letters only, so "YES—shown" reads YES and "NOTHING" is not NO.
_FIRST_WORD = re.compile(r"[A-Za-z]+")


def verify_candidate(
    vision_config: LLMConfig,
    parse_result: PixelParseResult,
    candidate: PixelUIElement,
    description: str,
) -> Verdict:
    """Ask the vision model to confirm a matched candidate before acting on it."""
    question = (
        f"Box [{candidate.index}] (type={candidate.type}, "
        f'content="{candidate.content}") was selected as the element matching: '
        f"'{description}'. Is box {candidate.index} really that element? "
        "Reply YES or NO on the first line, then one short reason."
    )
    messages = (
        ChatMessage(role="system", content=_CANDIDATE_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=(
                ImageContent(base64_data=parse_result.annotated_image),
                TextContent(text=question),
            ),
        ),
    )
    response = send_chat_completion(vision_config, messages)
    return _parse_verdict(response.message.content)


def verify_action_outcome(
    vision_config: LLMConfig,
    before_image_base64: str,
    after_image_base64: str,
    action_summary: str,
    expected_result: str,
) -> OutcomeVerdict:
    """Ask the vision model to compare before/after screenshots against the expected result."""
    question = (
        f"The agent {action_summary}. Expected result: {expected_result}. "
        "Comparing the BEFORE and AFTER screenshots, reply YES if that result "
        "is shown, CHANGED if the intended element was acted on and the screen "
        "changed but not to that result, or NO if nothing changed or a "
        "different element was acted on. One word on the first line, then one "
        "short reason."
    )
    messages = (
        ChatMessage(role="system", content=_OUTCOME_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=(
                TextContent(text="BEFORE:"),
                ImageContent(base64_data=before_image_base64),
                TextContent(text="AFTER:"),
                ImageContent(base64_data=after_image_base64),
                TextContent(text=question),
            ),
        ),
    )
    response = send_chat_completion(vision_config, messages)
    return _parse_outcome_verdict(response.message.content)


def _parse_outcome_verdict(content: object) -> OutcomeVerdict:
    """Map the first word of the reply; anything unreadable counts as missed."""
    if not isinstance(content, str):
        return OutcomeVerdict(
            status="missed", reason="vision model returned no content"
        )

    lines = content.splitlines()
    first_line_index = _first_non_empty_line_index(lines)
    if first_line_index is None:
        return OutcomeVerdict(status="missed", reason=content.strip())

    first_line = lines[first_line_index].strip()
    remainder = "\n".join(lines[first_line_index + 1 :]).strip()
    match = _FIRST_WORD.match(first_line.lstrip("*_ "))
    status = _OUTCOME_WORDS.get(match.group().upper()) if match else None
    if status is None:
        return OutcomeVerdict(status="missed", reason=content.strip())
    return OutcomeVerdict(status=status, reason=remainder or first_line)


def _parse_verdict(content: object) -> Verdict:
    if not isinstance(content, str):
        return Verdict(accepted=False, reason="vision model returned no content")

    lines = content.splitlines()
    first_line_index = _first_non_empty_line_index(lines)
    if first_line_index is None:
        return Verdict(accepted=False, reason=content.strip())

    first_line = lines[first_line_index].strip()
    remainder = "\n".join(lines[first_line_index + 1 :]).strip()

    if first_line.upper().startswith("YES"):
        return Verdict(accepted=True, reason=remainder or first_line)
    if first_line.upper().startswith("NO"):
        return Verdict(accepted=False, reason=remainder or first_line)
    return Verdict(accepted=False, reason=content.strip())


def _first_non_empty_line_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line.strip():
            return index
    return None
