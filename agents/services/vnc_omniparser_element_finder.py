from __future__ import annotations

import base64
import logging
import re
from collections.abc import Callable

from agents.exceptions import VncElementNotFoundError
from agents.services.dmr_client import send_chat_completion
from agents.services.omniparser_client import parse_screenshot_remote
from agents.services.vnc_session import VncSessionManager
from agents.types import ChatMessage, DMRConfig, ImageContent, TextContent
from omniparser_wrapper.types import PixelUIElement

logger = logging.getLogger(__name__)

_ELEMENT_MATCHER_SYSTEM_PROMPT = (
    "You are a UI element matcher. You will be given an annotated screenshot "
    "with numbered bounding boxes around detected UI elements, along with a "
    "text list of those elements and a description of the element the user "
    "wants to interact with.\n\n"
    "Use BOTH the annotated image and the element list to find the best match.\n\n"
    "Reply with ONLY the number of the matching element.\n"
    "For example: 3\n\n"
    "If no element matches the description, reply with: NOT_FOUND"
)


def find_element_coordinates_omniparser(
    vnc_session: VncSessionManager,
    description: str,
    vision_config: DMRConfig,
    *,
    on_screenshot: Callable[[str, str], None] | None = None,
) -> tuple[int, int]:
    screenshot_bytes = vnc_session.capture_screen()
    image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    parse_result = parse_screenshot_remote(image_base64)

    if on_screenshot is not None:
        on_screenshot(parse_result.annotated_image, "vnc_omniparser")

    if not parse_result.elements:
        msg = f"OmniParser found no UI elements on screen for: {description}"
        raise VncElementNotFoundError(msg)

    matched = _match_element_by_description(
        parse_result.elements,
        parse_result.annotated_image,
        description,
        vision_config,
    )

    logger.debug(
        "OmniParser matched element [%d] '%s' at (%d, %d) for '%s'",
        matched.index,
        matched.content,
        matched.center_x,
        matched.center_y,
        description,
    )

    return matched.center_x, matched.center_y


def _match_element_by_description(
    elements: tuple[PixelUIElement, ...],
    annotated_image_base64: str,
    description: str,
    dmr_config: DMRConfig,
) -> PixelUIElement:
    element_list = _build_element_list(elements)
    messages = _build_match_messages(element_list, annotated_image_base64, description)

    response = send_chat_completion(dmr_config, messages)
    answer = response.message.content

    if not isinstance(answer, str):
        msg = f"DMR returned empty content when matching element: {description}"
        raise VncElementNotFoundError(msg)

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


def _build_element_list(elements: tuple[PixelUIElement, ...]) -> str:
    lines: list[str] = []
    for el in elements:
        lines.append(
            f'[{el.index}] type={el.type}, content="{el.content}", '
            f"center=({el.center_x}, {el.center_y}), "
            f"interactive={el.interactivity}"
        )
    return "\n".join(lines)


def _parse_match_response(
    answer: str,
    description: str,
    elements: tuple[PixelUIElement, ...],
) -> PixelUIElement:
    if "NOT_FOUND" in answer:
        msg = f"No OmniParser element matches description: {description}"
        raise VncElementNotFoundError(msg)

    match = re.search(r"(\d+)", answer)
    if match is None:
        msg = f"Could not parse element index from DMR response: {answer}"
        raise VncElementNotFoundError(msg)

    index = int(match.group(1))

    for el in elements:
        if el.index == index:
            return el

    msg = (
        f"DMR returned index {index} which does not match any detected "
        f"element (valid: {[e.index for e in elements]})"
    )
    raise VncElementNotFoundError(msg)
