"""Plain text of an LLM chat message. Pure: no ORM."""

from __future__ import annotations

from agents.types import ChatMessage, TextContent


def message_text(message: ChatMessage) -> str:
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    return "\n".join(
        part.text for part in content if isinstance(part, TextContent)
    ).strip()
