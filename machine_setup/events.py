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
from projects.models import ACTIVE_RUN_STATUSES, TestRun
from projects.runner_person_step_push import nudge_runner_person_steps
from runners.types import RunnerId


def setup_group(project_id: int) -> str:
    # machine_readiness.events.setup_socket_group repeats this string (that
    # app must not import machine_setup); keep the two equal.
    return f"machine_setup_{project_id}"


def _send(project_id: int, event: dict[str, object]) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(setup_group(project_id), event)


def _nudge_project_runners(project_id: int) -> None:
    """Nudge the window of every Test Runner an active run of the project is
    assigned to (one query): their help and manual discussions, and the
    project's live-turn blockers, may have changed. Never raises."""
    try:
        runner_ids = set(
            TestRun.objects.filter(
                project_id=project_id,
                status__in=ACTIVE_RUN_STATUSES,
                runner__isnull=False,
            ).values_list("runner_id", flat=True)
        )
    except Exception:
        return
    for runner_id in runner_ids:
        if runner_id is not None:
            nudge_runner_person_steps(RunnerId(runner_id))


def broadcast_message(project_id: int, payload: SetupMessagePayload) -> None:
    _send(project_id, {"type": "machine_setup.message", "message": payload})
    # The Test Runner window shows help discussions (coalesced by its
    # consumer); it never shows sub-agent rows.
    if payload["parent_id"] is None:
        _nudge_project_runners(project_id)


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
    _nudge_project_runners(project_id)


def broadcast_reset(project_id: int) -> None:
    _send(project_id, {"type": "machine_setup.reset"})


def broadcast_help(project_id: int, payload: HelpThreadPayload) -> None:
    """The help panel of a help thread changed (draft or the agent's suggestion)."""
    _send(project_id, {"type": "machine_setup.help", "help": payload})
    _nudge_project_runners(project_id)
