"""(De)serialization of a replayable LLM transcript (the machine setup chat).

Pure: no ORM and no ``agents.services`` import (that package's ``__init__``
imports ``projects.services``; see ``agents/checkpoint.py``).

The stored shape keeps everything the Responses API needs to replay a
conversation statelessly: text, function calls and their outputs, and the
encrypted reasoning items riding on assistant messages. Images never enter this
transcript (screenshot tools return text); one found is stored as a marker.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import cast

from agents.types import (
    ChatMessage,
    ContentPart,
    ImageContent,
    ReasoningItem,
    TextContent,
    ToolCall,
)

logger = logging.getLogger(__name__)

TRANSCRIPT_VERSION = 1

_ROLES = ("system", "user", "assistant", "tool")
_IMAGE_MARKER = "[image omitted]"


class _MalformedTranscript(Exception):
    pass


def transcript_to_json(messages: Sequence[ChatMessage]) -> dict[str, object]:
    return {
        "version": TRANSCRIPT_VERSION,
        "messages": [_message_to_json(message) for message in messages],
    }


def transcript_from_json(data: object) -> tuple[ChatMessage, ...] | None:
    """Parse a stored transcript; ``()`` when empty, ``None`` when unusable."""
    if not data:
        return ()
    try:
        root = _as_dict(data, "transcript")
        if root.get("version") != TRANSCRIPT_VERSION:
            raise _MalformedTranscript(f"unsupported version {root.get('version')!r}")
        return tuple(
            _parse_message(_as_dict(raw, "message"))
            for raw in _as_list(root.get("messages"), "messages")
        )
    except _MalformedTranscript as exc:
        logger.warning("Ignoring unusable LLM transcript: %s", exc)
        return None


def close_dangling_tool_calls(
    messages: Sequence[ChatMessage], note: str
) -> tuple[ChatMessage, ...]:
    """Answer every function call that has no output with ``note``.

    The API rejects a replay where a function call has no output, which is what
    a stopped or killed turn leaves behind. Placeholders go right after the
    call's existing outputs, before the next non-tool message.
    """
    result: list[ChatMessage] = []
    pending: list[str] = []

    def _flush() -> None:
        for call_id in pending:
            result.append(ChatMessage(role="tool", content=note, tool_call_id=call_id))
        pending.clear()

    for message in messages:
        if message.role == "tool":
            if message.tool_call_id in pending:
                pending.remove(message.tool_call_id)
            result.append(message)
            continue
        _flush()
        result.append(message)
        if message.role == "assistant" and message.tool_calls:
            pending.extend(call.tool_call_id for call in message.tool_calls)
    _flush()
    return tuple(result)


def _message_to_json(message: ChatMessage) -> dict[str, object]:
    return {
        "role": message.role,
        "content": _content_to_json(message.content),
        "tool_calls": (
            [
                {
                    "id": call.tool_call_id,
                    "name": call.tool_name,
                    "arguments": call.arguments,
                }
                for call in message.tool_calls
            ]
            if message.tool_calls is not None
            else None
        ),
        "tool_call_id": message.tool_call_id,
        "reasoning": (
            [
                {
                    "id": item.item_id,
                    "encrypted_content": item.encrypted_content,
                    "summary": list(item.summary),
                }
                for item in message.reasoning
            ]
            if message.reasoning is not None
            else None
        ),
        "phase": message.phase,
    }


def _content_to_json(content: str | tuple[ContentPart, ...] | None) -> object:
    if content is None or isinstance(content, str):
        return content
    texts: list[str] = []
    for part in content:
        if isinstance(part, TextContent):
            texts.append(part.text)
        elif isinstance(part, ImageContent):
            texts.append(_IMAGE_MARKER)
    return "\n".join(texts)


def _parse_message(item: dict[str, object]) -> ChatMessage:
    role = item.get("role")
    if role not in _ROLES:
        raise _MalformedTranscript(f"bad role {role!r}")
    return ChatMessage(
        role=cast(str, role),
        content=_optional_str(item.get("content"), "content"),
        tool_calls=_parse_tool_calls(item.get("tool_calls")),
        tool_call_id=_optional_str(item.get("tool_call_id"), "tool_call_id"),
        reasoning=_parse_reasoning(item.get("reasoning")),
        phase=_optional_str(item.get("phase"), "phase"),
    )


def _parse_tool_calls(value: object) -> tuple[ToolCall, ...] | None:
    if value is None:
        return None
    calls: list[ToolCall] = []
    for raw in _as_list(value, "tool_calls"):
        item = _as_dict(raw, "tool call")
        calls.append(
            ToolCall(
                tool_call_id=_as_str(item.get("id"), "tool call id"),
                tool_name=_as_str(item.get("name"), "tool call name"),
                arguments=_as_dict(item.get("arguments"), "tool call arguments"),
            )
        )
    return tuple(calls)


def _parse_reasoning(value: object) -> tuple[ReasoningItem, ...] | None:
    if value is None:
        return None
    items: list[ReasoningItem] = []
    for raw in _as_list(value, "reasoning"):
        item = _as_dict(raw, "reasoning item")
        items.append(
            ReasoningItem(
                item_id=_as_str(item.get("id"), "reasoning id"),
                encrypted_content=_as_str(
                    item.get("encrypted_content"), "encrypted_content"
                ),
                summary=tuple(
                    _as_str(text, "reasoning summary")
                    for text in _as_list(item.get("summary"), "reasoning summary")
                ),
            )
        )
    return tuple(items)


def _as_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _MalformedTranscript(f"{label} is not an object")
    return cast(dict[str, object], value)


def _as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise _MalformedTranscript(f"{label} is not a list")
    return cast(list[object], value)


def _as_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise _MalformedTranscript(f"{label} is not a string")
    return value


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, label)
