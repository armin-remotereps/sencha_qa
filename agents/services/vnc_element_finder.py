from __future__ import annotations

import base64
import logging
import re

from agents.services.dmr_client import send_chat_completion
from agents.services.omniparser_client import is_omniparser_configured
from agents.services.vnc_omniparser_element_finder import (
    find_element_coordinates_omniparser,
)
from agents.services.vnc_session import VncSessionManager
from agents.types import ChatMessage, DMRConfig, ImageContent, TextContent

logger = logging.getLogger(__name__)


from agents.exceptions import VncElementNotFoundError as VncElementNotFoundError


def find_element_coordinates(
    vnc_session: VncSessionManager,
    description: str,
    vision_config: DMRConfig,
) -> tuple[int, int]:
    if is_omniparser_configured():
        return find_element_coordinates_omniparser(
            vnc_session, description, vision_config
        )

    screenshot_bytes = vnc_session.capture_screen()
    image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
    return _query_vision_model(image_base64, description, vision_config)


def _query_vision_model(
    image_base64: str,
    description: str,
    vision_config: DMRConfig,
) -> tuple[int, int]:
    messages = (
        ChatMessage(
            role="system",
            content=(
                "You are a UI element locator. Given a screenshot and an element "
                "description, find the element and return its center coordinates.\n\n"
                "Reply with ONLY the coordinates in the format: x,y\n"
                "For example: 450,320\n\n"
                "If the element is not visible or cannot be found, reply with: NOT_FOUND\n"
                "If the description is ambiguous (multiple matches), reply with: AMBIGUOUS"
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                TextContent(text=f"Find the element: {description}"),
                ImageContent(base64_data=image_base64),
            ),
        ),
    )

    response = send_chat_completion(vision_config, messages)
    answer = response.message.content
    if not isinstance(answer, str):
        msg = f"Vision model returned no response for element: {description}"
        raise VncElementNotFoundError(msg)

    return _parse_coordinates(answer.strip(), description)


def _parse_coordinates(
    answer: str,
    description: str,
) -> tuple[int, int]:
    if answer.startswith("NOT_FOUND"):
        msg = f"Element not found on screen: {description}"
        raise VncElementNotFoundError(msg)

    if answer.startswith("AMBIGUOUS"):
        msg = f"Ambiguous element on screen: {description} — {answer}"
        raise VncElementNotFoundError(msg)

    match = re.search(r"(\d+)\s*,\s*(\d+)", answer)
    if match is None:
        msg = f"Could not parse coordinates from vision response: {answer}"
        raise VncElementNotFoundError(msg)

    x = int(match.group(1))
    y = int(match.group(2))
    return x, y
