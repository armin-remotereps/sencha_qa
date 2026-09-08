from __future__ import annotations

import logging
import re
from collections.abc import Callable

from agents.exceptions import ElementNotFoundError, RejectedElementChosenError
from agents.services.llm_client import send_chat_completion
from agents.types import (
    ChatMessage,
    ImageContent,
    LLMConfig,
    PixelParseResult,
    PixelUIElement,
    ScreenshotCallback,
    TextContent,
)
from projects.services import controller_find_elements

logger = logging.getLogger(__name__)

_ELEMENT_MATCHER_SYSTEM_PROMPT = (
    "You are a UI element matcher. You will be given an annotated screenshot "
    "with numbered bounding boxes around detected UI elements, along with a "
    "text list of those elements and a description of the element the user "
    "wants to interact with.\n\n"
    "Use BOTH the annotated image and the element list to find the best match.\n\n"
    "Reply with ONLY the number of the matching element.\n"
    "For example: 3\n\n"
    "Some numbered boxes may be listed as already ruled out; they are still "
    "drawn on the image but must never be chosen.\n"
    "If no element matches the description, reply with: NOT_FOUND"
)


def parse_screen(
    project_id: int,
    *,
    on_screenshot: ScreenshotCallback | None = None,
) -> PixelParseResult:
    """Run OmniParser on the current screen and publish the annotated image."""
    parse_result = controller_find_elements(project_id)

    if on_screenshot is not None:
        on_screenshot(parse_result.annotated_image, "controller_omniparser")

    return parse_result


def match_element(
    parse_result: PixelParseResult,
    description: str,
    vision_config: LLMConfig,
    *,
    is_excluded: Callable[[PixelUIElement], bool] | None = None,
) -> PixelUIElement:
    """Match the description against a parsed screen's elements, applying an exclusion filter."""
    if not parse_result.elements:
        msg = f"OmniParser found no UI elements on screen for: {description}"
        raise ElementNotFoundError(msg)

    candidates, excluded = _split_excluded(parse_result.elements, is_excluded)
    if not candidates:
        msg = (
            f"All {len(parse_result.elements)} detected elements were already "
            f"rejected for: {description}"
        )
        raise ElementNotFoundError(msg)

    matched = _match_element_by_description(
        parse_result.elements,
        parse_result.annotated_image,
        description,
        vision_config,
        excluded_indices=tuple(el.index for el in excluded),
    )
    if matched in excluded:
        raise RejectedElementChosenError(matched, description)

    logger.debug(
        "OmniParser matched element [%d] '%s' at (%d, %d) for '%s'",
        matched.index,
        matched.content,
        matched.center_x,
        matched.center_y,
        description,
    )

    return matched


def _split_excluded(
    elements: tuple[PixelUIElement, ...],
    is_excluded: Callable[[PixelUIElement], bool] | None,
) -> tuple[tuple[PixelUIElement, ...], tuple[PixelUIElement, ...]]:
    if is_excluded is None:
        return elements, ()
    excluded = tuple(el for el in elements if is_excluded(el))
    candidates = tuple(el for el in elements if el not in excluded)
    return candidates, excluded


def find_element_coordinates(
    project_id: int,
    description: str,
    vision_config: LLMConfig,
    *,
    on_screenshot: ScreenshotCallback | None = None,
) -> tuple[int, int]:
    parse_result = parse_screen(project_id, on_screenshot=on_screenshot)
    matched = match_element(parse_result, description, vision_config)
    return matched.center_x, matched.center_y


def _match_element_by_description(
    elements: tuple[PixelUIElement, ...],
    annotated_image_base64: str,
    description: str,
    vision_config: LLMConfig,
    *,
    excluded_indices: tuple[int, ...] = (),
) -> PixelUIElement:
    element_list = _build_element_list(elements, excluded_indices)
    messages = _build_match_messages(element_list, annotated_image_base64, description)

    response = send_chat_completion(vision_config, messages)
    answer = response.message.content

    if not isinstance(answer, str):
        msg = f"LLM returned empty content when matching element: {description}"
        raise ElementNotFoundError(msg)

    return _parse_match_response(answer.strip(), description, elements)


def _build_match_messages(
    element_list: str,
    annotated_image_base64: str,
    description: str,
) -> tuple[ChatMessage, ...]:
    return (
        ChatMessage(role="system", content=_ELEMENT_MATCHER_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=(
                ImageContent(base64_data=annotated_image_base64),
                TextContent(
                    text=(
                        f"Detected UI elements:\n{element_list}\n\n"
                        f"Find the element: {description}"
                    )
                ),
            ),
        ),
    )


def _build_element_list(
    elements: tuple[PixelUIElement, ...], excluded_indices: tuple[int, ...] = ()
) -> str:
    lines: list[str] = []
    for el in elements:
        if el.index in excluded_indices:
            continue
        lines.append(
            f'[{el.index}] type={el.type}, content="{el.content}", '
            f"center=({el.center_x}, {el.center_y}), "
            f"interactive={el.interactivity}"
        )
    if excluded_indices:
        ruled_out = ", ".join(f"[{index}]" for index in excluded_indices)
        lines.append(f"Already ruled out, do not choose: {ruled_out}")
    return "\n".join(lines)


def _parse_match_response(
    answer: str,
    description: str,
    elements: tuple[PixelUIElement, ...],
) -> PixelUIElement:
    if "NOT_FOUND" in answer:
        msg = f"No OmniParser element matches description: {description}"
        raise ElementNotFoundError(msg)

    match = re.search(r"(\d+)", answer)
    if match is None:
        msg = f"Could not parse element index from LLM response: {answer}"
        raise ElementNotFoundError(msg)

    index = int(match.group(1))

    for el in elements:
        if el.index == index:
            return el

    msg = (
        f"LLM returned index {index} which does not match any detected "
        f"element (valid: {[e.index for e in elements]})"
    )
    raise ElementNotFoundError(msg)
