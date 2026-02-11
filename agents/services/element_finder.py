from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from playwright.sync_api import Page

from agents.services.dmr_client import send_chat_completion
from agents.types import ChatMessage, DMRConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ElementFinderConfig:
    char_budget: int = 3000
    chunk_size: int = 25
    response_ambiguous: str = "AMBIGUOUS"
    response_not_found: str = "NOT_FOUND"


_CONFIG = _ElementFinderConfig()

# Backward-compatible aliases (used by tests)
_CHUNK_SIZE = _CONFIG.chunk_size
_ELEMENT_LIST_CHAR_BUDGET = _CONFIG.char_budget


class ElementNotFoundError(Exception):
    """Raised when no element matches the description."""


class AmbiguousElementError(Exception):
    """Raised when multiple elements match the description."""


_COLLECT_ELEMENTS_JS = """
() => {
    const selectors = 'a, button, input, select, textarea, [role], [onclick], [tabindex]';
    const elements = Array.from(document.querySelectorAll(selectors));

    const visible = elements.filter(el => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && style.opacity !== '0'
            && rect.width > 0
            && rect.height > 0;
    });

    const result = [];
    visible.forEach((el, idx) => {
        el.setAttribute('data-at-idx', String(idx));

        const info = {
            idx: idx,
            tag: el.tagName.toLowerCase(),
            text: (el.textContent || '').trim().substring(0, 100),
            role: el.getAttribute('role') || '',
            ariaLabel: el.getAttribute('aria-label') || '',
            placeholder: el.getAttribute('placeholder') || '',
            type: el.getAttribute('type') || '',
            name: el.getAttribute('name') || '',
            id: el.id || '',
            href: el.getAttribute('href') || '',
            value: el.value !== undefined ? String(el.value).substring(0, 50) : '',
        };
        result.push(info);
    });

    return result;
}
"""


def find_element_by_description(
    page: Page,
    description: str,
    dmr_config: DMRConfig,
) -> str:
    """Find an element matching a natural-language description.

    Returns:
        CSS selector string like ``[data-at-idx="5"]``.

    Raises:
        ElementNotFoundError: No element matches the description.
        AmbiguousElementError: Multiple elements match ambiguously.
    """
    raw_elements = _collect_page_elements(page)
    element_list = _build_element_list(raw_elements)

    if len(element_list) <= _CONFIG.char_budget:
        answer = _ask_ai_for_element(description, element_list, dmr_config)
        idx = _parse_ai_response(answer, description, max_idx=len(raw_elements) - 1)
        return f'[data-at-idx="{idx}"]'

    return _find_element_chunked(raw_elements, description, dmr_config)


def _find_element_chunked(
    raw_elements: Sequence[object],
    description: str,
    dmr_config: DMRConfig,
) -> str:
    """Find element by processing chunks and aggregating candidates."""
    chunks = _split_into_chunks(raw_elements, _CONFIG.chunk_size)
    candidates: list[int] = []

    for chunk in chunks:
        chunk_list = _build_element_list(chunk)
        try:
            answer = _ask_ai_for_element(description, chunk_list, dmr_config)
            max_idx = _max_idx_in_chunk(chunk)
            idx = _parse_ai_response(answer, description, max_idx=max_idx)
            candidates.append(idx)
        except (ElementNotFoundError, AmbiguousElementError):
            continue

    if len(candidates) == 0:
        msg = f"No element found matching: {description}"
        raise ElementNotFoundError(msg)

    if len(candidates) == 1:
        return f'[data-at-idx="{candidates[0]}"]'

    candidate_elements: list[object] = [
        el
        for el in raw_elements
        if isinstance(el, dict) and el.get("idx") in candidates
    ]
    candidate_list = _build_element_list(candidate_elements)
    answer = _ask_ai_for_element(description, candidate_list, dmr_config)
    max_idx = max(candidates)
    idx = _parse_ai_response(answer, description, max_idx=max_idx)
    return f'[data-at-idx="{idx}"]'


def _max_idx_in_chunk(chunk: Sequence[object]) -> int:
    """Return the maximum idx value in a chunk of elements."""
    max_val = 0
    for el in chunk:
        if isinstance(el, dict):
            idx_val = el.get("idx", 0)
            if isinstance(idx_val, int) and idx_val > max_val:
                max_val = idx_val
    return max_val


def _split_into_chunks(
    elements: Sequence[object], chunk_size: int
) -> list[Sequence[object]]:
    """Split elements into chunks of chunk_size."""
    return [elements[i : i + chunk_size] for i in range(0, len(elements), chunk_size)]


def _collect_page_elements(page: Page) -> list[object]:
    """Execute JS to collect all visible interactive elements."""
    raw_elements = page.evaluate(_COLLECT_ELEMENTS_JS)
    if not isinstance(raw_elements, list) or len(raw_elements) == 0:
        msg = "No interactive elements found on the page"
        raise ElementNotFoundError(msg)
    return raw_elements


def _ask_ai_for_element(
    description: str, element_list: str, dmr_config: DMRConfig
) -> str:
    """Ask the AI model to identify the element matching the description."""
    prompt = (
        f"Find the element matching this description: '{description}'\n\n"
        f"Available elements:\n{element_list}\n\n"
        "Reply with ONLY the index number of the matching element.\n"
        "If the description is ambiguous (multiple possible matches), "
        f"reply with '{_CONFIG.response_ambiguous}: <explanation>'.\n"
        f"If no element matches, reply with '{_CONFIG.response_not_found}'."
    )

    messages = (
        ChatMessage(
            role="system",
            content=(
                "You are a UI element finder. Given a list of page elements "
                "and a description, identify which element matches. "
                f"Reply with ONLY the index number, '{_CONFIG.response_ambiguous}: ...', "
                f"or '{_CONFIG.response_not_found}'."
            ),
        ),
        ChatMessage(role="user", content=prompt),
    )

    response = send_chat_completion(dmr_config, messages)
    answer = response.message.content
    if not isinstance(answer, str):
        msg = "AI returned no response for element finding"
        raise ElementNotFoundError(msg)
    return answer.strip()


def _parse_ai_response(answer: str, description: str, *, max_idx: int) -> int:
    """Parse the AI response into a validated element index."""
    if answer.startswith(_CONFIG.response_ambiguous):
        raise AmbiguousElementError(answer)

    if answer == _CONFIG.response_not_found:
        msg = f"No element found matching: {description}"
        raise ElementNotFoundError(msg)

    try:
        idx = int(answer)
    except ValueError:
        match = re.search(r"\d+", answer)
        if match:
            idx = int(match.group())
        else:
            msg = f"Could not parse element index from AI response: {answer}"
            raise ElementNotFoundError(msg) from None

    if idx < 0 or idx > max_idx:
        msg = f"Element index {idx} out of range (0-{max_idx})"
        raise ElementNotFoundError(msg)

    return idx


def _build_element_list(elements: Sequence[object]) -> str:
    """Build a numbered list of element descriptions for the AI."""
    lines: list[str] = []
    for item in elements:
        if not isinstance(item, dict):
            continue
        idx = item.get("idx", "?")
        tag = item.get("tag", "unknown")
        text = item.get("text", "")
        role = item.get("role", "")
        aria_label = item.get("ariaLabel", "")
        placeholder = item.get("placeholder", "")
        el_type = item.get("type", "")
        name = item.get("name", "")
        el_id = item.get("id", "")
        href = item.get("href", "")

        parts = [f"[{idx}] <{tag}>"]
        if text:
            parts.append(f'text="{text}"')
        if role:
            parts.append(f'role="{role}"')
        if aria_label:
            parts.append(f'aria-label="{aria_label}"')
        if placeholder:
            parts.append(f'placeholder="{placeholder}"')
        if el_type:
            parts.append(f'type="{el_type}"')
        if name:
            parts.append(f'name="{name}"')
        if el_id:
            parts.append(f'id="{el_id}"')
        if href:
            parts.append(f'href="{href}"')

        lines.append(" ".join(parts))
    return "\n".join(lines)
