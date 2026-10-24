"""A bounded, deterministic digest of what a failed sub-agent attempt did.

Pure: no ORM, no settings and no ``agents.services`` import (that package's
``__init__`` imports ``projects.services``; see ``agents/checkpoint.py``).

The digest lists every tool call of the attempt (name, compact arguments, the
head of its result) and the attempt's last observation, so the failure
evaluation, the recovery sub-agent and the retried step know what was already
done. It never calls an LLM.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final

from agents.types import ChatMessage, ContentPart, TextContent

ATTEMPT_DIGEST_MAX_CHARS: Final = 6000

_ARGS_MAX_CHARS = 300
_ARG_VALUE_MAX_CHARS = 150
_RESULT_HEAD_CHARS = 300
_OBSERVATION_MAX_CHARS = 600
_NO_RESULT = "(no result recorded)"
_ELLIPSIS = "…"


def build_attempt_digest(messages: Sequence[ChatMessage]) -> str:
    """Digest of ``messages``; ``""`` when there is no tool call or observation."""
    results = {
        message.tool_call_id: _content_text(message.content)
        for message in messages
        if message.role == "tool" and message.tool_call_id is not None
    }
    call_lines: list[str] = []
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            result = results.get(call.tool_call_id)
            head = (
                _NO_RESULT
                if result is None
                else _cut(_collapse(result), _RESULT_HEAD_CHARS)
            )
            call_lines.append(
                f"{len(call_lines) + 1}. {call.tool_name}"
                f"({_compact_args(call.arguments)}) -> {head}"
            )

    observation = _last_observation(messages)
    observation_line = f"Last observation: {observation}" if observation else ""
    budget = ATTEMPT_DIGEST_MAX_CHARS - (
        len(observation_line) + 1 if observation_line else 0
    )
    lines = _fit_lines(call_lines, budget)
    if observation_line:
        lines.append(observation_line)
    return "\n".join(lines)


def _fit_lines(lines: list[str], budget: int) -> list[str]:
    """Keep head and tail lines within ``budget`` chars (newlines included).

    Early calls are the actions a retry must not repeat (typing, submits); late
    calls show the state the attempt left the machine in.
    """
    if _joined_length(lines) <= budget:
        return list(lines)
    # The marker and the newlines on both of its sides, sized for the worst case.
    marker_room = len(_omitted_marker(len(lines))) + 2
    available = max(budget - marker_room, 0)
    head = _take(lines, available // 3)
    tail_budget = available - _joined_length(head)
    tail = _take(lines[len(head) :][::-1], tail_budget)[::-1]
    return [*head, _omitted_marker(len(lines) - len(head) - len(tail)), *tail]


def _omitted_marker(count: int) -> str:
    return f"{_ELLIPSIS} {count} tool calls omitted {_ELLIPSIS}"


def _take(lines: list[str], budget: int) -> list[str]:
    taken: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if taken else 0)
        if used + cost > budget:
            break
        taken.append(line)
        used += cost
    return taken


def _joined_length(lines: Sequence[str]) -> int:
    return sum(len(line) for line in lines) + max(len(lines) - 1, 0)


def _last_observation(messages: Sequence[ChatMessage]) -> str:
    assistants = [m for m in messages if m.role == "assistant"]
    for message in reversed(assistants):
        text = _collapse(_content_text(message.content))
        if text:
            return _cut(text, _OBSERVATION_MAX_CHARS)
    if assistants and assistants[-1].reasoning:
        summary = " ".join(
            part for item in assistants[-1].reasoning for part in item.summary
        )
        return _cut(_collapse(summary), _OBSERVATION_MAX_CHARS)
    return ""


def _content_text(content: str | tuple[ContentPart, ...] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(
        part.text if isinstance(part, TextContent) else "[image]" for part in content
    )


def _compact_args(arguments: dict[str, object]) -> str:
    shortened = {
        key: _cut(value, _ARG_VALUE_MAX_CHARS) if isinstance(value, str) else value
        for key, value in arguments.items()
    }
    text = json.dumps(shortened, ensure_ascii=False, separators=(",", ":"), default=str)
    return _cut(text, _ARGS_MAX_CHARS)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)] + _ELLIPSIS
