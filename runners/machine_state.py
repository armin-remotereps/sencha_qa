"""The per-runner machine badge state (a leaf: no service imports).

Moved from ``projects.services.MachineState`` / ``get_machine_state`` /
``connected_machine_state`` and the device rule of ``projects.runner_target``.
It imports only models, the visual engine types and settings, so
``projects.services``, the gates and the template helpers can all use it.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

from django.conf import settings
from django.utils import timezone

from projects.models import RunnerDeviceState
from runners.models import TestRunner
from visual_engine.types import VisualEngineState, VisualEngineStatus


class MachineState(StrEnum):
    """A Test Runner's connection/readiness state, as shown in the UI."""

    DISCONNECTED = "disconnected"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    VISUAL_ENGINE_OFFLINE = "offline"
    # A phone or tablet runner is connected but its device is not READY, or
    # its last device status is stale (``RUNNER_DEVICE_STALE_SECONDS``).
    DEVICE_OFFLINE = "device_offline"


_MACHINE_STATE_BY_ENGINE_STATE: Final[dict[VisualEngineState, MachineState]] = {
    VisualEngineState.READY: MachineState.READY,
    VisualEngineState.STARTING: MachineState.STARTING,
    VisualEngineState.FAILED: MachineState.FAILED,
    VisualEngineState.OFFLINE: MachineState.VISUAL_ENGINE_OFFLINE,
}

# Shown by the run and setup gates while a phone runner's device is not READY.
DEVICE_NOT_READY_MESSAGE: Final = (
    "Connect the phone or tablet and wait until the Test Runner shows it as "
    "connected."
)


def engine_machine_state(visual_engine: VisualEngineStatus) -> MachineState:
    """The state any connected desktop runner (or ready device) has right now.

    The visual engine is global; pages listing many runners compute it once.
    (Was ``projects.services.connected_machine_state``.)
    """
    return _MACHINE_STATE_BY_ENGINE_STATE[visual_engine.state]


def runner_machine_state(
    runner: TestRunner | None, visual_engine: VisualEngineStatus
) -> MachineState:
    """One badge state from the runner's connection, its device and the engine.

    ``None`` (no runner assigned or selected) reads DISCONNECTED. No query:
    reads ``connected``, ``device_state`` and ``device_reported_at``, which
    callers using ``.only()`` must load.
    """
    if runner is None or not runner.connected:
        return MachineState.DISCONNECTED
    device_state = device_machine_state(runner)
    if device_state is not None:
        return device_state
    return engine_machine_state(visual_engine)


def device_machine_state(runner: TestRunner) -> MachineState | None:
    """The state a connected phone runner's device imposes, or None when the
    device does not hold the badge (desktop, not connected, READY and fresh)."""
    if not device_offline(runner):
        return None
    if runner.device_state == RunnerDeviceState.STARTING:
        return MachineState.STARTING
    return MachineState.DEVICE_OFFLINE


def device_offline(runner: TestRunner) -> bool:
    """A connected phone runner whose device is not READY, or whose last
    device status is older than ``RUNNER_DEVICE_STALE_SECONDS`` (no query).

    False for a desktop runner and when the runner is not connected.
    """
    state = runner.device_state
    if not runner.connected or state == RunnerDeviceState.NONE:
        return False
    if state != RunnerDeviceState.READY:
        return True
    reported_at = runner.device_reported_at
    if reported_at is None:
        return True
    stale_after = timedelta(seconds=settings.RUNNER_DEVICE_STALE_SECONDS)
    return timezone.now() - reported_at > stale_after
