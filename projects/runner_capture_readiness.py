"""Whether the project's Test Runner can take a window capture right now.

docs/plan/feat-mcp-runner-capture.md (Server: controller action seam). A
leaf: it imports only the models, the shared protocol, ``runner_target`` and
``project_capture_apps_services`` (itself a leaf: models, exceptions,
protocol), so the MCP gateway's services can use it without the legacy
services. One
query; nothing is cached.
"""

from __future__ import annotations

from dataclasses import dataclass

from controller_client.protocol import ClientCapability
from projects.models import Project, RunnerDeviceState
from projects.project_capture_apps_services import stored_capture_apps
from projects.runner_target import device_offline


@dataclass(frozen=True)
class CaptureReadiness:
    # A Test Runner holds the project's controller connection.
    connected: bool
    # It advertised WINDOW_CAPTURE_V1 (False when not connected: outdated
    # only means something while connected).
    supported: bool
    # It drives a phone or tablet (non-blank ``agent_device_state``).
    phone: bool
    # A phone runner's device is READY and fresh; always True for a desktop
    # runner and when not connected.
    device_ready: bool
    # The project's allowlist, normalized; empty: runner capture is off.
    allowed: tuple[str, ...]


def capture_readiness(project_id: int) -> CaptureReadiness:
    """The capture readiness of the project (one query).

    Raises ``Project.DoesNotExist`` for an unknown project.
    """
    project = Project.objects.only(
        "agent_connected",
        "agent_capabilities",
        "agent_device_state",
        "agent_device_reported_at",
        "capture_allowed_processes",
    ).get(pk=project_id)
    connected = project.agent_connected
    capabilities = project.agent_capabilities
    supported = (
        connected
        and isinstance(capabilities, list)
        and ClientCapability.WINDOW_CAPTURE_V1.value in capabilities
    )
    allowed = tuple(stored_capture_apps(project))
    return CaptureReadiness(
        connected=connected,
        supported=supported,
        phone=connected and project.agent_device_state != RunnerDeviceState.NONE,
        # The one device rule (``runner_target.device_offline``), no query.
        device_ready=not device_offline(project),
        allowed=allowed,
    )
