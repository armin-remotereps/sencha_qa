"""Composition root of ``machine_readiness.runner_ports.RunnerPorts``.

``machine_readiness`` must not import ``projects.services`` or
``machine_setup`` (plan D2), so the ``projects`` views and tasks that scan or
check the machine, and ``machine_setup``'s readiness tools, build its runner
ports here and pass them in. Every port acts on one Test Runner.
"""

from __future__ import annotations

from collections.abc import Sequence

from machine_readiness.runner_ports import (
    AppCheckResult,
    DirectoryScanResult,
    PathStat,
    ReadinessWork,
    RunnerBusy,
    RunnerPorts,
)
from projects.runner_use import (
    RunnerHoldKind,
    agent_run_started_on_runner,
    runner_hold,
)
from projects.services import (
    controller_check_app_installed,
    controller_inspect_paths,
    controller_scan_directory,
)
from runners.services.runner_access import runner_serves_project_id
from runners.types import RunnerId


def build_runner_ports() -> RunnerPorts:
    return RunnerPorts(
        scan_directory=_scan_directory,
        inspect_paths=_inspect_paths,
        check_app_installed=_check_app_installed,
        runner_busy=_runner_busy,
    )


# What a counted holder means to a scan or a check.
_BUSY_BY_HOLD: dict[RunnerHoldKind, RunnerBusy] = {
    RunnerHoldKind.AGENT_RUN: RunnerBusy.OTHER_PROJECT,
    RunnerHoldKind.MANUAL_CASE: RunnerBusy.MANUAL_TESTER,
    RunnerHoldKind.SETUP_TURN: RunnerBusy.SETUP_RUNNING,
    RunnerHoldKind.RUNNER_UPDATE: RunnerBusy.UPDATING,
    RunnerHoldKind.READINESS_SCAN: RunnerBusy.SCANNING,
    RunnerHoldKind.READINESS_CHECK: RunnerBusy.CHECKING,
}
_OWN_LOCK: dict[ReadinessWork, RunnerHoldKind] = {
    ReadinessWork.SCAN: RunnerHoldKind.READINESS_SCAN,
    ReadinessWork.CHECK: RunnerHoldKind.READINESS_CHECK,
}


def _runner_busy(
    runner_id: RunnerId,
    project_id: int,
    *,
    work: ReadinessWork,
    ignore_turn_id: int | None,
) -> RunnerBusy | None:
    """Why a scan or check (``work``) of ``project_id`` may not use the runner.

    The runner must still serve the project (not archived, its owner an
    active writable member: ``runner_serves_project``); then a Started agent
    run, then every ``projects.runner_use`` holder
    except the asker's own: its own readiness lock (a queued scan holds it
    when it re-checks; the other readiness lock still refuses, so a scan and
    a check never overlap on one runner), the asking setup turn, and a
    paused agent run of this project (it leaves its machine to Check machine
    and Machine setup). A handful of cheap queries and two cache reads.
    """
    if not runner_serves_project_id(runner_id, project_id):
        return RunnerBusy.NOT_SERVING
    if agent_run_started_on_runner(runner_id):
        return RunnerBusy.RUN_STARTED
    own_lock = _OWN_LOCK[work]
    hold = runner_hold(
        runner_id,
        ignore_turn_id=ignore_turn_id,
        allow=lambda held: held.kind is own_lock
        or (held.kind is RunnerHoldKind.AGENT_RUN and held.project_id == project_id),
    )
    return None if hold is None else _BUSY_BY_HOLD[hold.kind]


def _scan_directory(
    runner_id: RunnerId,
    path: str,
    *,
    max_depth: int,
    max_entries: int,
    allow_network: bool,
    timeout: float,
) -> DirectoryScanResult:
    scan = controller_scan_directory(
        runner_id,
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
    runner_id: RunnerId,
    paths: Sequence[str],
    *,
    allow_network: bool,
    timeout: float,
) -> list[PathStat]:
    return [
        PathStat(path=info.path, exists=info.exists, is_dir=info.is_dir)
        for info in controller_inspect_paths(
            runner_id, paths, allow_network=allow_network, timeout=timeout
        )
    ]


def _check_app_installed(
    runner_id: RunnerId, app_name: str, *, timeout: float
) -> AppCheckResult:
    result = controller_check_app_installed(runner_id, app_name, timeout=timeout)
    return AppCheckResult(success=bool(result["success"]), message=result["message"])
