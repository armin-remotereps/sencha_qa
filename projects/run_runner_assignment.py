"""Assign a run to a Test Runner, or switch it to another one (Q6).

- ``assign_runner``: a WAITING run (picked before Start). Start and the new run
  of a Re-run take the same rule inside their own transaction through
  ``lock_runner_to_assign`` + ``write_runner_assignment``.
- ``reassign_runner``: a PAUSED, DONE or CANCELLED run ("Change runner"). A
  STARTED run cannot switch; pause it first. A switch bumps
  ``execution_generation`` so stale chain tasks stand down, and sets
  ``runner_assigned_at``, which withholds the ``current_screen`` continue mode
  of older help requests (``case_pause_policy``).

Lock order (every acquiring service): runner row(s), then project (while the
Phase 1 per-project rule lives), then run. A reassign locks both runners in id
order. Ownership: only the runner's owner assigns it (Q2); any writable member
then acts through the run (Q3) with ``lock_usable_runner``, while the runner's
owner is still an active writable member of the project.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from functools import partial
from typing import Final

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.turn_state import run_auto_fix_turn_live
from projects.models import Project, TestRun, TestRunStatus
from projects.runner_person_step_push import nudge_runner_person_steps
from projects.runner_use import runner_hold, runner_id_of_run
from projects.screen_continuations import drop_screen_continuations
from runners.exceptions import (
    RunAssignmentError,
    RunnerHeldError,
    RunnerNotFoundError,
    RunnerNotReadyError,
    RunnerPermissionError,
)
from runners.locks import lock_runner_row
from runners.machine_state import DEVICE_NOT_READY_MESSAGE, device_offline
from runners.models import TestRunner
from runners.services.runner_access import can_assign, runner_serves_project
from runners.types import RunnerId

ASSIGNABLE_RUN_STATUSES: Final[frozenset[str]] = frozenset({TestRunStatus.WAITING})
# Q6: a run may switch runner only while it is not executing.
REASSIGNABLE_RUN_STATUSES: Final[frozenset[str]] = frozenset(
    {TestRunStatus.PAUSED, TestRunStatus.DONE, TestRunStatus.CANCELLED}
)

RUNNER_NOT_CONNECTED_MESSAGE: Final = "The Test Runner is not connected."
RUN_STARTED_MESSAGE: Final = (
    "The run is executing. Pause it before you change its Test Runner."
)
RUN_NOT_ASSIGNABLE_MESSAGE: Final = "This run's Test Runner cannot be changed now."
RUNNER_CHANGED_MESSAGE: Final = (
    "The run's Test Runner changed meanwhile. Reload the page and try again."
)
AUTO_FIX_LIVE_MESSAGE: Final = (
    "The setup agent is fixing a case of this run on its Test Runner. Stop "
    "the auto-fix or wait for it to end, then change the runner."
)
RUNNER_NOT_SERVING_MESSAGE: Final = (
    "This Test Runner's owner can no longer use it for this project. Choose "
    "another Test Runner."
)


def lock_usable_runner(
    runner_id: RunnerId, *, project_id: int, ignore_run_id: int | None = None
) -> TestRunner:
    """Lock the runner row and check it can serve a holder of ``project_id`` now.

    Call inside ``transaction.atomic`` before locking any project or run.
    Raises ``RunnerNotFoundError`` (missing or archived),
    ``RunnerPermissionError`` (its owner is no longer an active writable
    member of the project: ``runner_access.runner_serves_project``),
    ``RunnerNotReadyError`` (not connected, or its phone is not ready) or
    ``RunnerHeldError`` (another holder; ``ignore_run_id`` is the asking run).
    Does not require the actor to own it: Resume and help Continue by any
    writable member of the run's project use it on the run's own runner (Q3).
    The visual engine is left to the callers' readiness checks.
    """
    runner = lock_runner_row(runner_id)
    if runner is None or runner.archived_at is not None:
        raise RunnerNotFoundError()
    if not runner_serves_project(runner, project_id):
        raise RunnerPermissionError(RUNNER_NOT_SERVING_MESSAGE)
    if not runner.connected:
        raise RunnerNotReadyError(RUNNER_NOT_CONNECTED_MESSAGE)
    if device_offline(runner):
        raise RunnerNotReadyError(DEVICE_NOT_READY_MESSAGE)
    hold = runner_hold(runner.runner_id, ignore_run_id=ignore_run_id)
    if hold is not None:
        raise RunnerHeldError(hold.message)
    return runner


def lock_runner_to_assign(
    runner_id: RunnerId,
    *,
    actor: CustomUser,
    project: Project,
    ignore_run_id: int | None = None,
) -> TestRunner:
    """``lock_usable_runner`` plus ``runner_access.can_assign`` (owner and a
    writable member of the project); raises ``RunnerPermissionError``."""
    runner = lock_usable_runner(
        runner_id, project_id=project.pk, ignore_run_id=ignore_run_id
    )
    if not can_assign(actor, runner, project):
        raise RunnerPermissionError(
            "You can only choose your own Test Runners for this project."
        )
    return runner


def write_runner_assignment(
    test_run: TestRun, runner: TestRunner, *, now: datetime | None = None
) -> None:
    """Write the run's runner fields (the run row must be locked by the caller)
    and nudge the old and new runner windows after commit."""
    previous = runner_id_of_run(test_run)
    test_run.runner = runner
    test_run.runner_name_snapshot = runner.name
    test_run.runner_assigned_at = now or timezone.now()
    test_run.save(
        update_fields=[
            "runner",
            "runner_name_snapshot",
            "runner_assigned_at",
            "updated_at",
        ]
    )
    _nudge_after_commit([previous, runner.runner_id])


def assign_runner(
    *, test_run: TestRun, runner_id: RunnerId, actor: CustomUser
) -> TestRun:
    """Assign a WAITING run to the actor's runner.

    Raises the ``lock_runner_to_assign`` errors and ``RunAssignmentError``
    when the run is no longer WAITING. Same runner again: no change.
    """
    with transaction.atomic():
        runner = lock_runner_to_assign(runner_id, actor=actor, project=test_run.project)
        run = TestRun.objects.select_for_update().get(pk=test_run.pk)
        if run.status not in ASSIGNABLE_RUN_STATUSES:
            raise RunAssignmentError(RUN_NOT_ASSIGNABLE_MESSAGE)
        if run.runner_id != runner.pk:
            write_runner_assignment(run, runner)
    return run


def reassign_runner(
    *, test_run: TestRun, runner_id: RunnerId, actor: CustomUser
) -> TestRun:
    """Switch a PAUSED, DONE or CANCELLED run to the actor's runner (Q6).

    Locks the old and new runner rows in id order, then the project and the
    run, and checks under the run lock that the run still has the old runner
    it locked. Bumps ``execution_generation`` and sets
    ``runner_assigned_at``, and makes the run's queued cases restore
    instead of going on from the old screen (``drop_screen_continuations``).
    Raises the ``lock_runner_to_assign`` errors and
    ``RunAssignmentError`` (STARTED or WAITING run, the runner changed
    meanwhile, or a Night auto-fix of the run is live on its machine). Same
    runner again: no change. Handing back a waiting Human in the loop step
    is the caller's (``run_runner_change``, in the same transaction).
    """
    old_runner_id = runner_id_of_run(test_run)
    other_id = old_runner_id if old_runner_id not in (None, runner_id) else None
    with transaction.atomic():
        if other_id is not None and other_id < runner_id:
            lock_runner_row(other_id, fields=("id",))
        runner = lock_runner_to_assign(
            runner_id,
            actor=actor,
            project=test_run.project,
            ignore_run_id=test_run.pk,
        )
        if other_id is not None and other_id > runner_id:
            lock_runner_row(other_id, fields=("id",))
        Project.objects.select_for_update().only("id").get(pk=test_run.project_id)
        run = TestRun.objects.select_for_update().get(pk=test_run.pk)
        if runner_id_of_run(run) not in (old_runner_id, runner_id):
            # Switched or released since the old runner was read: its row
            # was not the one locked above.
            raise RunAssignmentError(RUNNER_CHANGED_MESSAGE)
        if run.status == TestRunStatus.STARTED:
            raise RunAssignmentError(RUN_STARTED_MESSAGE)
        if run.status not in REASSIGNABLE_RUN_STATUSES:
            raise RunAssignmentError(RUN_NOT_ASSIGNABLE_MESSAGE)
        if run.runner_id == runner.pk:
            return run
        if run_auto_fix_turn_live(run.pk):
            raise RunAssignmentError(AUTO_FIX_LIVE_MESSAGE)
        TestRun.objects.filter(pk=run.pk).update(
            execution_generation=F("execution_generation") + 1
        )
        write_runner_assignment(run, runner)
        # Queued cases (a report or help Continue whose queueing failed)
        # must not go on from the old machine's screen (or none at all).
        drop_screen_continuations(run)
        run.refresh_from_db(fields=["execution_generation"])
    return run


def _nudge_after_commit(runner_ids: Iterable[RunnerId | None]) -> None:
    for runner_id in {rid for rid in runner_ids if rid is not None}:
        transaction.on_commit(partial(nudge_runner_person_steps, runner_id))
