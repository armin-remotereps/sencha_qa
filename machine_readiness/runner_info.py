"""The connected Test Runner as machine readiness sees it (no runner call)."""

from __future__ import annotations

from dataclasses import dataclass

from controller_client.protocol import (
    ClientCapability,
    RunnerTarget,
    parse_runner_target,
)
from machine_readiness.models import ArtifactOs
from machine_readiness.profile_rules import artifact_os_for
from machine_readiness.run_stage0_state import runner_host
from machine_readiness.runner_ports import advertises
from projects.models import Project


@dataclass(frozen=True)
class RunnerInfo:
    connected: bool
    # The profile OS of the runner's reported OS; None when unsupported.
    os_name: ArtifactOs | None
    # ``run_stage0_state.runner_host``; None when unknown.
    host: str | None
    # ARTIFACT_SCAN_V1 advertised (scan_directory, inspect_paths).
    scan_supported: bool
    # What the runner drives: desktop, or a phone/tablet (``os_name`` and the
    # artifacts stay the host's either way).
    target: RunnerTarget


def runner_info_of(project: Project) -> RunnerInfo:
    """From a loaded project row (no query)."""
    return _runner_info(
        project.agent_connected, project.agent_system_info, project.agent_capabilities
    )


def load_runner_info(project_id: int) -> RunnerInfo:
    """One query."""
    connected, system_info, capabilities = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_connected", "agent_system_info", "agent_capabilities")
        .get()
    )
    return _runner_info(connected, system_info, capabilities)


def _runner_info(
    connected: object, system_info: object, capabilities: object
) -> RunnerInfo:
    os_raw = system_info.get("os") if isinstance(system_info, dict) else None
    # D4 (docs/plan/feat-mobile-device-runner.md): a phone target needs the
    # ``target`` key and MOBILE_DEVICE_V1; anything else is a desktop runner.
    target = (
        parse_runner_target(system_info)
        if isinstance(system_info, dict)
        and advertises(capabilities, ClientCapability.MOBILE_DEVICE_V1)
        else RunnerTarget.DESKTOP
    )
    return RunnerInfo(
        connected=bool(connected),
        os_name=artifact_os_for(os_raw) if isinstance(os_raw, str) else None,
        host=runner_host(system_info),
        scan_supported=advertises(capabilities, ClientCapability.ARTIFACT_SCAN_V1),
        target=target,
    )
