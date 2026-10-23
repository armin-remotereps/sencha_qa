"""JSON payloads of chat rows and turns, shared by the page, socket and broadcasts.

Pure builders over already-loaded rows: callers fetch ``author`` and
``screenshots`` up front (select_related / Prefetch).
"""

from __future__ import annotations

import json
from typing import TypedDict

from django.conf import settings

from machine_setup.models import SetupMessage, SetupScreenshot, SetupTurn

_ARGUMENTS_TEXT_LIMIT = 4000


class SetupScreenshotPayload(TypedDict):
    id: int
    url: str
    tool_name: str


class SetupMessagePayload(TypedDict):
    id: int
    conversation_id: int
    turn_id: int | None
    kind: str
    text: str
    author_name: str
    tool_name: str
    tool_arguments_text: str
    tool_status: str
    screenshots: list[SetupScreenshotPayload]
    link_url: str
    created_at: str


class SetupTurnPayload(TypedDict):
    id: int
    conversation_id: int
    status: str
    status_label: str
    ended_reason: str
    ended_reason_label: str
    error_message: str
    stop_requested: bool
    iterations: int
    max_iterations: int


def screenshot_payload(screenshot: SetupScreenshot) -> SetupScreenshotPayload:
    return {
        "id": screenshot.pk,
        "url": screenshot.image.url,
        "tool_name": screenshot.tool_name,
    }


def message_payload(
    message: SetupMessage, screenshots: list[SetupScreenshot] | None = None
) -> SetupMessagePayload:
    """``screenshots`` defaults to the prefetched ``message.screenshots``."""
    rows = screenshots if screenshots is not None else list(message.screenshots.all())
    author = message.author
    return {
        "id": message.pk,
        "conversation_id": message.conversation_id,
        "turn_id": message.turn_id,
        "kind": message.kind,
        "text": message.text,
        "author_name": (author.get_full_name() or author.email) if author else "",
        "tool_name": message.tool_name,
        "tool_arguments_text": _arguments_text(message.tool_arguments),
        "tool_status": message.tool_status,
        "screenshots": [screenshot_payload(row) for row in rows],
        "link_url": message.link_url,
        "created_at": message.created_at.isoformat(),
    }


def turn_payload(turn: SetupTurn) -> SetupTurnPayload:
    return {
        "id": turn.pk,
        "conversation_id": turn.conversation_id,
        "status": turn.status,
        "status_label": turn.get_status_display(),
        "ended_reason": turn.ended_reason,
        "ended_reason_label": (
            turn.get_ended_reason_display() if turn.ended_reason else ""
        ),
        "error_message": turn.error_message,
        "stop_requested": turn.stop_requested,
        "iterations": turn.iterations,
        "max_iterations": settings.MACHINE_SETUP_MAX_ITERATIONS,
    }


def _arguments_text(arguments: object) -> str:
    if not arguments:
        return ""
    text = json.dumps(arguments, indent=2, ensure_ascii=False, default=str)
    if len(text) > _ARGUMENTS_TEXT_LIMIT:
        return text[:_ARGUMENTS_TEXT_LIMIT] + "\n…"
    return text
