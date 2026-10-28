from __future__ import annotations

import logging

from agents.services.llm_client import send_chat_completion
from agents.types import ChatMessage, ImageContent, LLMConfig, TextContent

logger = logging.getLogger(__name__)


def answer_screenshot_question(
    vision_config: LLMConfig,
    image_base64: str,
    question: str,
    *,
    image_size: tuple[int, int] | None = None,
) -> str:
    """Send a screenshot to the vision model with a specific question.

    Args:
        vision_config: LLM configuration for the vision model.
        image_base64: Base64-encoded PNG screenshot.
        question: Question to ask about the screenshot.
        image_size: The screenshot's (width, height) in pixels. When given, the
            model is told the size and asked for pixel coordinates, which
            click_at and scroll take.

    Returns:
        Text answer from the vision model.
    """
    system_prompt = (
        "Answer the question based on what you see in the screenshot. "
        "Be concise and precise."
    )
    if image_size is not None:
        width, height = image_size
        system_prompt += (
            f" The screenshot is {width} x {height} pixels. Give any screen "
            "position as pixel coordinates (x, y) from its top-left corner."
        )
    messages = (
        ChatMessage(role="system", content=system_prompt),
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
