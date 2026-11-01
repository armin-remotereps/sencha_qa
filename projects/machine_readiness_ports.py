"""Composition root of ``machine_readiness.runner_ports.RunnerPorts``.

``machine_readiness`` must not import ``projects.services`` or
``machine_setup`` (plan D2), so the ``projects`` views and tasks that scan or
check the machine, and ``machine_setup``'s readiness tools, build its runner
ports here and pass them in.
"""

from __future__ import annotations

from collections.abc import Sequence

from machine_readiness.runner_ports import (
    AppCheckResult,
    DirectoryScanResult,
    PathStat,
    RunnerPorts,
)
from machine_setup.turn_state import live_turn_exists
from projects.run_machine_use import agent_run_started, manual_tester_on_machine
from projects.runner_update_state import runner_update_in_progress
from projects.services import (
    controller_check_app_installed,
    controller_inspect_paths,
    controller_scan_directory,
)


def build_runner_ports() -> RunnerPorts:
    return RunnerPorts(
        scan_directory=_scan_directory,
        inspect_paths=_inspect_paths,
        check_app_installed=_check_app_installed,
        live_turn_exists=live_turn_exists,
        runner_update_in_progress=runner_update_in_progress,
        agent_run_started=agent_run_started,
        manual_tester_on_machine=manual_tester_on_machine,
    )


def _scan_directory(
    project_id: int,
    path: str,
    *,
    max_depth: int,
    max_entries: int,
    allow_network: bool,
    timeout: float,
) -> DirectoryScanResult:
    scan = controller_scan_directory(
        project_id,
        path,
        max_depth=max_depth,
        max_entries=max_entries,
        allow_network=allow_network,
        timeout=timeout,
    )
    return DirectoryScanResult(
        root=scan.root,
        entries=scan.entries,
        truncated=scan.truncated,
        permission_errors=scan.permission_errors,
        error=scan.error,
    )


def _inspect_paths(
    project_id: int,
    paths: Sequence[str],
    *,
    allow_network: bool,
    timeout: float,
) -> list[PathStat]:
    return [
        PathStat(path=info.path, exists=info.exists, is_dir=info.is_dir)
        for info in controller_inspect_paths(
            project_id, paths, allow_network=allow_network, timeout=timeout
        )
    ]


def _check_app_installed(
    project_id: int, app_name: str, *, timeout: float
) -> AppCheckResult:
    result = controller_check_app_installed(project_id, app_name, timeout=timeout)
    return AppCheckResult(success=bool(result["success"]), message=result["message"])
