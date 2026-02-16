from __future__ import annotations

import logging
import re
from collections.abc import Callable

from agents.exceptions import ElementNotFoundError
from agents.services.controller_omniparser_element_finder import (
    find_element_coordinates_omniparser,
)
from agents.services.dmr_client import send_chat_completion
from agents.services.omniparser_client import is_omniparser_configured
from agents.types import ChatMessage, DMRConfig, ImageContent, TextContent
from projects.services import controller_screenshot

logger = logging.getLogger(__name__)


def find_element_coordinates(
    project_id: int,
    description: str,
    vision_config: DMRConfig,
    *,
    on_screenshot: Callable[[str, str], None] | None = None,
) -> tuple[int, int]:
    if is_omniparser_configured():
        return find_element_coordinates_omniparser(
            project_id, description, vision_config, on_screenshot=on_screenshot
        )

    result = controller_screenshot(project_id)
    image_base64 = result["image_base64"]
    if on_screenshot is not None:
        on_screenshot(image_base64, "controller_element_finder")
    return _query_vision_model(image_base64, description, vision_config)


def _query_vision_model(
    image_base64: str,
    description: str,
    vision_config: DMRConfig,
) -> tuple[int, int]:
    messages = _build_locator_messages(image_base64, description)
    answer = _send_locator_query(vision_config, messages, description)
    return _parse_coordinates(answer, description)


def _build_locator_messages(
    image_base64: str,
    description: str,
) -> tuple[ChatMessage, ...]:
    return (
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


def _send_locator_query(
    vision_config: DMRConfig,
    messages: tuple[ChatMessage, ...],
    description: str,
) -> str:
    response = send_chat_completion(vision_config, messages)
    answer = response.message.content
    if not isinstance(answer, str):
        msg = f"Vision model returned no response for element: {description}"
        raise ElementNotFoundError(msg)
    return answer.strip()


def _parse_coordinates(
    answer: str,
    description: str,
) -> tuple[int, int]:
    if answer.startswith("NOT_FOUND"):
        msg = f"Element not found on screen: {description}"
        raise ElementNotFoundError(msg)

    if answer.startswith("AMBIGUOUS"):
        msg = f"Ambiguous element on screen: {description} — {answer}"
        raise ElementNotFoundError(msg)

    match = re.search(r"(\d+)\s*,\s*(\d+)", answer)
    if match is None:
        msg = f"Could not parse coordinates from vision response: {answer}"
        raise ElementNotFoundError(msg)

    x = int(match.group(1))
    y = int(match.group(2))
    return x, y
