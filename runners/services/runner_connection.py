"""Ownership of a Test Runner's live controller socket.

Ported from ``projects.services`` (``claim_agent_connection``,
``release_agent_connection``, ``force_disconnect_controller``,
``_clear_agent_connection``, ``abort_active_test_run_on_disconnect``), keyed by
runner instead of project:

- the newest authenticated socket of a runner always wins; the caller (the
  controller consumer) closes the replaced channel by sending it
  ``runners.channels.SUPERSEDED_EVENT`` (close code 4001);
- a late disconnect of an older socket releases the runner only while it still
  owns it, so a reconnect is never wiped by its predecessor's cleanup;
- Force disconnect clears the state first, closes the socket through the
  ``runner_{id}`` group (``FORCE_DISCONNECT_EVENT``, close code 4002) and aborts
  the agent runs STARTED on the runner.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from controller_client.protocol import ClientCapability, is_mobile_target
from projects.models import RunHelpMode, RunnerDeviceState, TestRun, TestRunStatus
from projects.services import abort_test_run
from runners.channels import FORCE_DISCONNECT_EVENT, runner_group
from runners.exceptions import RunnerNotFoundError
from runners.models import TestRunner
from runners.services.runner_status import broadcast_runner_status
from runners.types import RunnerId

logger = logging.getLogger(__name__)

RUNNER_DISCONNECTED_REASON = "Test Runner disconnected"


@dataclass(frozen=True)
class RunnerClaim:
    """A claimed connection: the refreshed runner and the channel it replaced
    ("" when there was none or it was the same channel)."""

    runner: TestRunner
    superseded_channel: str


class _DisconnectedFields(TypedDict):
    connected: bool
    channel_name: str
    system_info: dict[str, object]
    client_version: str
    capabilities: list[str]
    device_state: str
    device_detail: str
    device_reported_at: datetime | None
    last_connected_at: datetime
    updated_at: datetime


def _disconnected_fields() -> _DisconnectedFields:
    now = timezone.now()
    return {
        "connected": False,
        "channel_name": "",
        "system_info": {},
        "client_version": "",
        "capabilities": [],
        "device_state": RunnerDeviceState.NONE.value,
        "device_detail": "",
        "device_reported_at": None,
        "last_connected_at": now,
        "updated_at": now,
    }


def claim_runner_connection(
    runner_id: RunnerId,
    channel_name: str,
    system_info: dict[str, Any],
    client_version: str,
    capabilities: Sequence[str],
) -> RunnerClaim:
    """Make ``channel_name`` the owner of the runner's controller connection.

    A phone or tablet runner (``target`` in ``system_info`` and
    MOBILE_DEVICE_V1; the consumer drops the device keys otherwise) starts
    with the device ``starting`` until its first ``device_status``; any other
    runner has a blank device state. Raises ``RunnerNotFoundError`` when the
    runner was archived since the handshake authenticated it.
    """
    phone = (
        is_mobile_target(system_info)
        and ClientCapability.MOBILE_DEVICE_V1.value in capabilities
    )
    now = timezone.now()
    with transaction.atomic():
        row = (
            TestRunner.objects.select_for_update()
            .filter(pk=runner_id, archived_at__isnull=True)
            .values_list("channel_name", flat=True)
            .first()
        )
        if row is None:
            raise RunnerNotFoundError()
        previous_channel_name = row
        TestRunner.objects.filter(pk=runner_id).update(
            connected=True,
            channel_name=channel_name,
            system_info=system_info,
            client_version=client_version,
            capabilities=list(capabilities),
            device_state=(
                RunnerDeviceState.STARTING if phone else RunnerDeviceState.NONE
            ),
            device_detail="",
            device_reported_at=now if phone else None,
            updated_at=now,
        )
    runner = TestRunner.objects.get(pk=runner_id)
    superseded = "" if previous_channel_name == channel_name else previous_channel_name
    return RunnerClaim(runner=runner, superseded_channel=superseded)


def release_runner_connection(
    runner_id: RunnerId, channel_name: str
) -> TestRunner | None:
    """Mark the runner disconnected only if ``channel_name`` still owns it.

    Returns the refreshed runner when the state changed (so the caller
    broadcasts it), or None when a newer connection already took over.
    """
    rows_updated = TestRunner.objects.filter(
        pk=runner_id, channel_name=channel_name
    ).update(**_disconnected_fields())
    if rows_updated == 0:
        return None
    return TestRunner.objects.get(pk=runner_id)


def clear_runner_connection(runner_id: RunnerId) -> None:
    """Mark the runner disconnected whoever owns it (no close, no abort).

    Used before a close event is sent, so a waiter woken by the closing
    consumer's CONTROLLER_DISCONNECTED reply never sees the dead connection.
    """
    TestRunner.objects.filter(pk=runner_id).update(**_disconnected_fields())


def send_runner_close(runner_id: RunnerId) -> None:
    """Ask the runner's live socket to close with 4002 (never raises)."""
    layer: Any = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            runner_group(runner_id), {"type": FORCE_DISCONNECT_EVENT}
        )
    except Exception:
        logger.exception("Close event failed for runner %s", runner_id)


def force_disconnect_runner(runner: TestRunner) -> bool:
    """Force-disconnect the runner's socket and abort the runs it executes.

    Clears the connection first, then closes the socket (4002), broadcasts the
    new state and aborts every STARTED agent run assigned to the runner (A21;
    a PAUSED run executes nothing, and a Manual run's tester keeps working
    without the runner). Returns False when the runner was not connected.
    The caller checks ``runner_access.can_force_disconnect``.
    """
    if not runner.connected:
        return False
    clear_runner_connection(runner.runner_id)
    send_runner_close(runner.runner_id)
    runner.refresh_from_db()
    broadcast_runner_status(runner)
    abort_runs_on_runner(runner.runner_id)
    return True


def abort_runs_on_runner(runner_id: RunnerId) -> None:
    """Abort the STARTED agent runs assigned to the runner."""
    runs = TestRun.objects.filter(
        runner_id=runner_id, status=TestRunStatus.STARTED
    ).exclude(help_mode=RunHelpMode.MANUAL)
    for test_run in runs:
        abort_test_run(test_run, reason=RUNNER_DISCONNECTED_REASON)
