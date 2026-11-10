"""The run's Test Runner on the paths that put a run (back) to work.

Start, Resume, Re-run in place, help Continue, "Continue run" and person-step
answers all need "the run's runner is set, connected, compatible and not held
by someone else" (plan Q3, Q6). Each path does it in two parts:

1. ``select_run_runner`` (outside the caller's transaction): a requested
   runner other than the run's is switched to with
   ``run_runner_assignment.reassign_runner`` (owner only, Q2; its own
   transaction, which locks the runners and then the run). An agent run with
   no runner, and none requested, is refused with ``CHOOSE_RUNNER_MESSAGE``.
2. ``lock_run_runner`` (first thing inside the caller's transaction, before
   the project and run locks: the lock order is runner, project, run): the
   runner row is locked and checked usable by any writable member (Q3,
   ``lock_usable_runner``). ``check_run_runner_unchanged`` then verifies,
   under the run lock, that the run still has that runner.

Every runner refusal is raised as the caller's own domain error (``error``),
so views map one error type per path. It imports models, the assignment
module and leaves only, never ``projects.services``; ``projects.services``
imports it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from accounts.models import CustomUser
from projects.manual_run_policy import is_manual_mode
from projects.models import TestRun
from projects.run_runner_assignment import lock_usable_runner, reassign_runner
from projects.runner_use import runner_id_of_run
from runners.exceptions import RunnerError
from runners.locks import lock_runner_row
from runners.models import TestRunner
from runners.types import RunnerId

CHOOSE_RUNNER_MESSAGE: Final = "Choose a Test Runner for this run."
RUNNER_REMOVED_MESSAGE: Final = "The run's Test Runner was removed. Choose another one."
RUNNER_CHANGED_MESSAGE: Final = (
    "The run's Test Runner changed meanwhile. Reload the page and try again."
)
# The user's Test Runners page (``runners`` app); readiness checks link it.
RUNNERS_PAGE_URL_NAME: Final = "runners:list"

ErrorFactory = Callable[[str], Exception]


def select_run_runner(
    test_run: TestRun,
    *,
    runner_id: RunnerId | None,
    actor: CustomUser | None,
    error: ErrorFactory,
) -> RunnerId | None:
    """The runner the run continues on, switching to ``runner_id`` if asked.

    Call outside any transaction that holds a project or run lock.
    ``runner_id`` None keeps the run's runner; an agent run without one is
    refused (``CHOOSE_RUNNER_MESSAGE``). A Manual run may stay without one
    (Q12). A different runner is assigned through ``reassign_runner``
    (``actor`` must own it; the run must be PAUSED, DONE or CANCELLED), which
    bumps the execution generation and withholds the current-screen continue
    mode of older help requests. ``test_run.runner*`` and
    ``execution_generation`` are refreshed in memory after a switch.
    """
    current = runner_id_of_run(test_run)
    if runner_id is not None and runner_id != current:
        if actor is None:
            raise error("Only a signed-in member can choose a Test Runner.")
        try:
            switched = reassign_runner(
                test_run=test_run, runner_id=runner_id, actor=actor
            )
        except RunnerError as exc:
            raise error(exc.message) from exc
        test_run.runner_id = switched.runner_id
        test_run.runner_name_snapshot = switched.runner_name_snapshot
        test_run.runner_assigned_at = switched.runner_assigned_at
        test_run.execution_generation = switched.execution_generation
        return runner_id
    if current is None and not is_manual_mode(test_run.help_mode):
        raise error(CHOOSE_RUNNER_MESSAGE)
    return current


def lock_run_runner(
    runner_id: RunnerId | None,
    *,
    test_run_id: int,
    project_id: int,
    error: ErrorFactory,
    usable: bool = True,
) -> TestRunner | None:
    """Lock the run's runner row (None: nothing to lock, a Manual run).

    Call inside the caller's transaction before ``lock_project_and_run``.
    ``usable``: also check it still serves ``project_id`` (its owner an
    active writable member), is connected, its phone ready and not held by
    another holder (``lock_usable_runner``; the run itself does not count).
    Without it only the row is locked, for paths whose gates check the runner
    themselves under the lock (``run_reopen_services.list_wake_blockers``,
    which can ignore a live auto-fix turn that the action is about to stop).
    """
    if runner_id is None:
        return None
    if not usable:
        runner = lock_runner_row(runner_id)
        if runner is None:
            raise error(RUNNER_REMOVED_MESSAGE)
        return runner
    try:
        return lock_usable_runner(
            runner_id, project_id=project_id, ignore_run_id=test_run_id
        )
    except RunnerError as exc:
        raise error(exc.message) from exc


def check_run_runner_unchanged(
    test_run: TestRun, runner_id: RunnerId | None, *, error: ErrorFactory
) -> None:
    """Under the run lock: the run still has the runner locked for it."""
    if runner_id_of_run(test_run) != runner_id:
        raise error(RUNNER_CHANGED_MESSAGE)
