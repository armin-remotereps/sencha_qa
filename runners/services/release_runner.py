"""The owner releases a runner from the active runs assigned to it.

Each run is handed to ``projects.run_runner_release.release_run_runner``,
which owns the run-side rules (a STARTED agent run is refused: pause it
first; a Manual claim is released; the run's runner is cleared). This module
checks ownership, refuses up front while an agent run executes on the runner
(so nothing is released half way), and finds the runs.
"""

from __future__ import annotations

from accounts.models import CustomUser
from projects.models import ACTIVE_RUN_STATUSES, RunHelpMode, TestRun, TestRunStatus
from projects.run_runner_release import (
    RUN_STARTED_RELEASE_MESSAGE,
    release_run_runner,
)
from runners.exceptions import (
    RunAssignmentError,
    RunnerNotFoundError,
    RunnerPermissionError,
)
from runners.models import TestRunner
from runners.services.runner_access import can_manage
from runners.types import RunnerId


def release_runner(*, actor: CustomUser, runner_id: RunnerId) -> int:
    """Release every active run assigned to the actor's runner.

    Returns how many runs were released (0 when none held it). Raises
    ``RunnerNotFoundError``, ``RunnerPermissionError``, or
    ``RunAssignmentError`` while an agent run is executing on it (pause it
    first; nothing is released); run-side refusals
    raised by ``release_run_runner`` propagate unchanged.
    """
    runner = TestRunner.objects.filter(pk=runner_id).first()
    if runner is None:
        raise RunnerNotFoundError()
    if not can_manage(actor, runner):
        raise RunnerPermissionError()
    runs = list(
        TestRun.objects.filter(
            runner_id=runner.pk, status__in=ACTIVE_RUN_STATUSES
        ).order_by("pk")
    )
    if any(
        run.status == TestRunStatus.STARTED and run.help_mode != RunHelpMode.MANUAL
        for run in runs
    ):
        raise RunAssignmentError(RUN_STARTED_RELEASE_MESSAGE)
    for test_run in runs:
        release_run_runner(test_run=test_run, actor=actor)
    return len(runs)
