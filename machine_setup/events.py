"""Channel-layer broadcasts to a project's machine setup page."""

from __future__ import annotations

from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from machine_setup.payloads import (
    HelpThreadPayload,
    SetupMessagePayload,
    SetupScreenshotPayload,
    SetupTurnPayload,
)


def setup_group(project_id: int) -> str:
    return f"machine_setup_{project_id}"


def _send(project_id: int, event: dict[str, object]) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(setup_group(project_id), event)


def broadcast_message(project_id: int, payload: SetupMessagePayload) -> None:
    _send(project_id, {"type": "machine_setup.message", "message": payload})


def broadcast_screenshot(
    project_id: int, message_id: int, screenshot: SetupScreenshotPayload
) -> None:
    _send(
        project_id,
        {
            "type": "machine_setup.screenshot",
            "message_id": message_id,
            "screenshot": screenshot,
        },
    )


def broadcast_turn(project_id: int, payload: SetupTurnPayload) -> None:
    _send(project_id, {"type": "machine_setup.turn", "turn": payload})


def broadcast_reset(project_id: int) -> None:
    _send(project_id, {"type": "machine_setup.reset"})


def broadcast_help(project_id: int, payload: HelpThreadPayload) -> None:
    """The help panel of a help thread changed (draft or the agent's suggestion)."""
    _send(project_id, {"type": "machine_setup.help", "help": payload})
