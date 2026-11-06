"""What the project's Test Runner drives: the desktop or a phone or tablet.

A leaf (docs/plan/feat-mobile-device-runner.md, D4 and D6): it imports only
the models, the shared protocol and settings, so the legacy services, the
gates and template helpers can all use it. The controller consumer stores
``target`` and the device keys in ``agent_system_info`` only for a runner
that advertised MOBILE_DEVICE_V1, so ``parse_runner_target`` on the stored
system info is the target. ``claim_agent_connection`` sets the device state
to ``starting`` for a phone runner and blank for any other, so a non-blank
``agent_device_state`` marks a phone runner without reading the JSON.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from django.conf import settings
from django.utils import timezone

from controller_client.protocol import RunnerTarget, parse_runner_target
from projects.models import Project, RunnerDeviceState

# Shown by the run and setup gates while a phone runner's device is not READY.
DEVICE_NOT_READY_MESSAGE: Final = (
    "Connect the phone or tablet and wait until the Test Runner shows it as "
    "connected."
)


def load_runner_target(project_id: int) -> RunnerTarget:
    """The connected runner's target (one query); desktop when none is."""
    system_info = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_system_info", flat=True)
        .first()
    )
    return _target_of(system_info)


def project_runner_target(project: Project) -> RunnerTarget:
    """The target from the loaded ``agent_system_info`` (no query)."""
    return _target_of(project.agent_system_info)


def _target_of(system_info: object) -> RunnerTarget:
    if not isinstance(system_info, dict):
        return RunnerTarget.DESKTOP
    return parse_runner_target(system_info)


def device_offline(project: Project) -> bool:
    """A connected phone runner whose device is not READY, or whose last
    device status is older than ``RUNNER_DEVICE_STALE_SECONDS`` (no query).

    False for a desktop runner and when no runner is connected. Reads
    ``agent_connected``, ``agent_device_state`` and
    ``agent_device_reported_at``: callers loading projects with ``.only()``
    must include them.
    """
    state = project.agent_device_state
    if not project.agent_connected or state == RunnerDeviceState.NONE:
        return False
    if state != RunnerDeviceState.READY:
        return True
    reported_at = project.agent_device_reported_at
    if reported_at is None:
        return True
    stale_after = timedelta(seconds=settings.RUNNER_DEVICE_STALE_SECONDS)
    return timezone.now() - reported_at > stale_after
