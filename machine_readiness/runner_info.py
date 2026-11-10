"""A Test Runner as machine readiness sees it (no runner call)."""

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
from runners.models import TestRunner
from runners.types import RunnerId


@dataclass(frozen=True)
class RunnerInfo:
    # The Test Runner described; None when none is assigned or picked.
    runner_id: RunnerId | None
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


NO_RUNNER_INFO = RunnerInfo(
    runner_id=None,
    connected=False,
    os_name=None,
    host=None,
    scan_supported=False,
    target=RunnerTarget.DESKTOP,
)


def runner_info_of(runner: TestRunner | None) -> RunnerInfo:
    """From a loaded runner row (no query); None gives ``NO_RUNNER_INFO``.

    Reads ``connected``, ``system_info`` and ``capabilities``.
    """
    if runner is None:
        return NO_RUNNER_INFO
    return _runner_info(
        runner.runner_id, runner.connected, runner.system_info, runner.capabilities
    )


def load_runner_info(runner_id: RunnerId | None) -> RunnerInfo:
    """One query (none for no runner); a deleted runner reads as none."""
    if runner_id is None:
        return NO_RUNNER_INFO
    row = (
        TestRunner.objects.filter(pk=runner_id)
        .values_list("connected", "system_info", "capabilities")
        .first()
    )
    if row is None:
        return NO_RUNNER_INFO
    connected, system_info, capabilities = row
    return _runner_info(runner_id, connected, system_info, capabilities)


def _runner_info(
    runner_id: RunnerId, connected: object, system_info: object, capabilities: object
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
        runner_id=runner_id,
        connected=bool(connected),
        os_name=artifact_os_for(os_raw) if isinstance(os_raw, str) else None,
        host=runner_host(system_info),
        scan_supported=advertises(capabilities, ClientCapability.ARTIFACT_SCAN_V1),
        target=target,
    )
