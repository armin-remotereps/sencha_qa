"""Whether a Test Runner can take a window capture for a project right now.

docs/plan/feat-mcp-runner-capture.md (Server: controller action seam). A
leaf: it imports only the models, the shared protocol, ``runner_target`` and
``project_capture_apps_services`` (itself a leaf: models, exceptions,
protocol), so the MCP gateway's services can use it without the legacy
services. Two queries at most; nothing is cached.

The allowlist is the project's; the runner is the caller's (the runner of the
claimed case's run for MCP). No runner reads as not connected.
"""

from __future__ import annotations

from dataclasses import dataclass

from controller_client.protocol import ClientCapability
from projects.models import Project, RunnerDeviceState
from projects.project_capture_apps_services import stored_capture_apps
from projects.runner_target import device_offline
from runners.models import TestRunner
from runners.types import RunnerId


@dataclass(frozen=True)
class CaptureReadiness:
    # The runner is connected (False when there is no runner).
    connected: bool
    # It advertised WINDOW_CAPTURE_V1 (False when not connected: outdated
    # only means something while connected).
    supported: bool
    # It drives a phone or tablet (non-blank ``device_state``).
    phone: bool
    # A phone runner's device is READY and fresh; always True for a desktop
    # runner and when not connected.
    device_ready: bool
    # The project's allowlist, normalized; empty: runner capture is off.
    allowed: tuple[str, ...]


def capture_readiness(project_id: int, runner_id: RunnerId | None) -> CaptureReadiness:
    """The capture readiness of ``runner_id`` for the project (≤ 2 queries).

    Raises ``Project.DoesNotExist`` for an unknown project. A missing or
    archived runner, or None, reads as not connected.
    """
    project = Project.objects.only("capture_allowed_processes").get(pk=project_id)
    allowed = tuple(stored_capture_apps(project))
    runner = (
        TestRunner.objects.filter(pk=runner_id, archived_at__isnull=True)
        .only("connected", "capabilities", "device_state", "device_reported_at")
        .first()
        if runner_id is not None
        else None
    )
    if runner is None or not runner.connected:
        return CaptureReadiness(
            connected=False,
            supported=False,
            phone=False,
            device_ready=True,
            allowed=allowed,
        )
    capabilities = runner.capabilities
    return CaptureReadiness(
        connected=True,
        supported=(
            isinstance(capabilities, list)
            and ClientCapability.WINDOW_CAPTURE_V1.value in capabilities
        ),
        phone=runner.device_state != RunnerDeviceState.NONE,
        # The one device rule (``runner_target.device_offline``), no query.
        device_ready=not device_offline(runner),
        allowed=allowed,
    )
