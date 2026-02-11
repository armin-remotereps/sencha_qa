from __future__ import annotations

import logging

from agents.services.dmr_client import send_chat_completion
from agents.types import ChatMessage, DMRConfig, ImageContent, TextContent

logger = logging.getLogger(__name__)


def answer_screenshot_question(
    vision_config: DMRConfig,
    image_base64: str,
    question: str,
) -> str:
    """Send a screenshot to the vision model with a specific question.

    Args:
        vision_config: DMR configuration for the vision model.
        image_base64: Base64-encoded PNG screenshot.
        question: Question to ask about the screenshot.

    Returns:
        Text answer from the vision model.
    """
    messages = (
        ChatMessage(
            role="system",
            content=(
                "Answer the question based on what you see in the screenshot. "
                "Be concise and precise."
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                TextContent(text=question),
                ImageContent(base64_data=image_base64),
            ),
        ),
    )
    response = send_chat_completion(vision_config, messages)
    content = response.message.content
    return content if isinstance(content, str) else "Unable to answer the question."
