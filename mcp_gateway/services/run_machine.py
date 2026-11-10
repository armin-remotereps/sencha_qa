"""The test machine of a Manual run: which runner the MCP tools reach, and
whether something other than the run's own claims holds it
(docs/plan/feat-user-test-runners.md A16).

A Manual run's runner is optional (Q12): ``None`` means the machine and
capture tools answer ``[not_set_up]``. The runner serves at most one holder
at a time (``projects.runner_use``); a claimed, unpaused case of the same
run is the MCP tools' own hold, anything else answers ``[machine_busy]``.
The runner's name reaches an MCP reply only as "test machine <name>"
(``machine_label``, Q9).
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp_gateway.untrusted_text import case_line
from projects.models import RunHelpMode, TestRun, TestRunStatus
from projects.runner_use import RunnerHoldKind, runner_hold
from runners.models import TestRunner
from runners.services.runner_access import runner_serves_project
from runners.types import RunnerId

# What the MCP tools read of a runner (no token or owner column).
RUNNER_FIELDS: tuple[str, ...] = (
    "runner__id",
    "runner__owner_id",
    "runner__name",
    "runner__archived_at",
    "runner__connected",
    "runner__system_info",
    "runner__capabilities",
    "runner__device_state",
    "runner__device_reported_at",
)


@dataclass(frozen=True)
class ManualRunMachine:
    """The project's running Manual run and its runner (None: no runner)."""

    run_id: int
    run_name: str
    runner: TestRunner | None


def load_manual_run_machine(project_id: int) -> ManualRunMachine | None:
    """The project's running Manual run with its runner, or None when no
    Manual run is running (two queries at most). A runner that no longer
    serves the project (archived, owner gone: ``runner_serves_project``)
    reads as none. Phase 1 keeps at most one active run per project."""
    run = (
        TestRun.objects.filter(
            project_id=project_id,
            help_mode=RunHelpMode.MANUAL,
            status=TestRunStatus.STARTED,
        )
        .select_related("runner")
        .only("id", "name", "runner_id", *RUNNER_FIELDS)
        .order_by("-started_at", "-pk")
        .first()
    )
    if run is None:
        return None
    runner = run.runner
    if runner is not None and not runner_serves_project(runner, project_id):
        # Archived, or its owner no longer serves the project: no machine.
        runner = None
    return ManualRunMachine(run_id=run.pk, run_name=run.name, runner=runner)


def serving_runner_id(runner_id: RunnerId | None, project_id: int) -> RunnerId | None:
    """``runner_id`` while that runner still serves the project, else None
    (one query; none for no runner): the MCP tools then answer
    ``[not_set_up]``, never why."""
    if runner_id is None:
        return None
    runner = (
        TestRunner.objects.filter(pk=runner_id)
        .only("id", "owner_id", "archived_at")
        .first()
    )
    if runner is None or not runner_serves_project(runner, project_id):
        return None
    return runner_id


def held_by_other(runner_id: RunnerId, test_run_id: int) -> bool:
    """Something other than a claimed case of ``test_run_id`` holds the
    runner: another run, a Machine setup turn, a self-update, or a readiness
    Scan / Check machine (``runner_hold``: a handful of cheap queries and two
    cache reads)."""
    hold = runner_hold(
        runner_id,
        allow=lambda held: held.kind is RunnerHoldKind.MANUAL_CASE
        and held.test_run_id == test_run_id,
    )
    return hold is not None


def machine_label(name: str) -> str:
    """The only form a runner's name takes in an MCP reply (Q9), one line of
    data."""
    return case_line(f"test machine {name}")
