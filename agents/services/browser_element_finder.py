from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from agents.exceptions import ElementNotFoundError
from agents.services.dmr_client import send_chat_completion
from agents.types import ChatMessage, DMRConfig
from projects.services import controller_browser_get_elements

logger = logging.getLogger(__name__)

_AI_RESPONSE_AMBIGUOUS = "AMBIGUOUS"
_AI_RESPONSE_NOT_FOUND = "NOT_FOUND"

_ELEMENT_LIST_CHAR_BUDGET = 3000
_CHUNK_SIZE = 25


def find_element_index(
    project_id: int,
    description: str,
    dmr_config: DMRConfig,
) -> int:
    result = controller_browser_get_elements(project_id)
    element_list = result["content"]

    if not element_list.strip():
        raise ElementNotFoundError("No interactive elements found on the page")

    if len(element_list) <= _ELEMENT_LIST_CHAR_BUDGET:
        answer = _ask_ai_for_element(description, element_list, dmr_config)
        max_idx = _extract_max_index(element_list)
        return _parse_ai_response(answer, description, max_idx)

    return _find_element_chunked(element_list, description, dmr_config)


def _find_element_chunked(
    element_list: str,
    description: str,
    dmr_config: DMRConfig,
) -> int:
    lines = element_list.strip().split("\n")
    chunks = _split_into_chunks(lines, _CHUNK_SIZE)
    candidates: list[int] = []

    for chunk in chunks:
        chunk_text = "\n".join(chunk)
        try:
            answer = _ask_ai_for_element(description, chunk_text, dmr_config)
            max_idx = _extract_max_index(chunk_text)
            idx = _parse_ai_response(answer, description, max_idx)
            candidates.append(idx)
        except ElementNotFoundError:
            continue

    if len(candidates) == 0:
        raise ElementNotFoundError(f"No element found matching: {description}")

    if len(candidates) == 1:
        return candidates[0]

    candidate_lines = [
        line for line in lines if _line_matches_any_index(line, candidates)
    ]
    candidate_text = "\n".join(candidate_lines)
    answer = _ask_ai_for_element(description, candidate_text, dmr_config)
    max_idx = max(candidates)
    return _parse_ai_response(answer, description, max_idx)


def _line_matches_any_index(line: str, indices: list[int]) -> bool:
    match = re.match(r"\[(\d+)\]", line)
    if match is None:
        return False
    return int(match.group(1)) in indices


def _extract_max_index(element_list: str) -> int:
    indices = [int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", element_list)]
    return max(indices) if indices else 0


def _ask_ai_for_element(
    description: str, element_list: str, dmr_config: DMRConfig
) -> str:
    prompt = (
        f"Find the element matching this description: '{description}'\n\n"
        f"Available elements:\n{element_list}\n\n"
        "Reply with ONLY the index number of the matching element.\n"
        "If the description is ambiguous (multiple possible matches), "
        f"reply with '{_AI_RESPONSE_AMBIGUOUS}: <explanation>'.\n"
        f"If no element matches, reply with '{_AI_RESPONSE_NOT_FOUND}'."
    )

    messages = (
        ChatMessage(
            role="system",
            content=(
                "You are a UI element finder. Given a list of page elements "
                "and a description, identify which element matches. "
                f"Reply with ONLY the index number, '{_AI_RESPONSE_AMBIGUOUS}: ...', "
                f"or '{_AI_RESPONSE_NOT_FOUND}'."
            ),
        ),
        ChatMessage(role="user", content=prompt),
    )

    response = send_chat_completion(dmr_config, messages)
    answer = response.message.content
    if not isinstance(answer, str):
        raise ElementNotFoundError("AI returned no response for element finding")
    return answer.strip()


def _parse_ai_response(answer: str, description: str, max_idx: int) -> int:
    if answer.startswith(_AI_RESPONSE_AMBIGUOUS):
        raise ElementNotFoundError(f"Ambiguous element: {answer}")

    if answer == _AI_RESPONSE_NOT_FOUND:
        raise ElementNotFoundError(f"No element found matching: {description}")

    match = re.search(r"(\d+)", answer)
    if match is None:
        raise ElementNotFoundError(
            f"Could not parse element index from AI response: {answer}"
        )

    idx = int(match.group())
    if idx < 0 or idx > max_idx:
        raise ElementNotFoundError(f"Element index {idx} out of range (0-{max_idx})")
    return idx


def _split_into_chunks(items: Sequence[str], chunk_size: int) -> list[Sequence[str]]:
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]
