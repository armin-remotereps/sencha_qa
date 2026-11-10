"""Release a run's Test Runner: its owner takes the machine back (plan Q3).

Owner only (``runner_access.can_manage``). Locks the runner row, then the
project and the run (the lock order of every runner holder), and clears the
run's runner fields, which ends the run's hold (``runner_use``):

- a STARTED agent run: refused (``RUN_STARTED_RELEASE_MESSAGE``); pause it
  first, as a runner change requires (``run_runner_assignment``).
- a PAUSED agent run: it stays paused; Continue asks for a runner and the
  current-screen mode is withheld (``case_pause_policy.help_screen_trusted``).
- a Manual run: the claims made at the runner's window are released (the
  cases go back to Created); MCP claims keep their case, and their machine
  tools answer "not set up" until a runner is assigned (Q12).
- a finished run: only the assignment is cleared.

The old runner's window is nudged after commit.
"""

from __future__ import annotations

from functools import partial

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.exceptions import ManualRunError
from projects.manual_case_claim_services import release_manual_case
from projects.manual_run_policy import is_manual_run
from projects.models import (
    MANUAL_SOURCE_MCP,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_actor import PersonStepActor
from projects.run_reopen_services import lock_project_and_run
from projects.runner_person_step_push import nudge_runner_person_steps
from projects.services import broadcast_test_run_status
from runners.exceptions import RunAssignmentError, RunnerPermissionError
from runners.locks import lock_runner_row
from runners.services.runner_access import can_manage
from runners.types import RunnerId

_NO_RUNNER = "This run has no Test Runner to release."
_OWNER_ONLY = "Only the Test Runner's owner can release it."
RUN_STARTED_RELEASE_MESSAGE = "Pause the run first, then release its Test Runner."


def release_run_runner(*, test_run: TestRun, actor: CustomUser) -> TestRun:
    """Take the run's Test Runner back from it; returns the locked run.

    Raises ``RunAssignmentError`` when the run has no runner (or it changed
    meanwhile) or is a STARTED agent run (pause it first), and
    ``RunnerPermissionError`` unless ``actor`` owns it.
    """
    runner_id = test_run.runner_id
    if runner_id is None:
        raise RunAssignmentError(_NO_RUNNER)
    with transaction.atomic():
        runner = lock_runner_row(RunnerId(runner_id), fields=("id", "owner_id"))
        if runner is None:
            raise RunAssignmentError(_NO_RUNNER)
        if not can_manage(actor, runner):
            raise RunnerPermissionError(_OWNER_ONLY)
        _, run = lock_project_and_run(test_run.pk)
        if run.runner_id != runner.pk:
            raise RunAssignmentError(_NO_RUNNER)
        if run.status == TestRunStatus.STARTED and not is_manual_run(run):
            raise RunAssignmentError(RUN_STARTED_RELEASE_MESSAGE)
        run.runner = None
        run.runner_name_snapshot = ""
        # Withholds the current-screen continue mode of open help requests.
        run.runner_assigned_at = timezone.now()
        run.save(
            update_fields=[
                "runner",
                "runner_name_snapshot",
                "runner_assigned_at",
                "updated_at",
            ]
        )
        if is_manual_run(run):
            _release_window_claims(run, actor=actor)
        transaction.on_commit(partial(nudge_runner_person_steps, RunnerId(runner.pk)))
        transaction.on_commit(partial(broadcast_test_run_status, run))
    return run


def _release_window_claims(test_run: TestRun, *, actor: CustomUser) -> None:
    """Release the claims made at the Test Runner window (not through MCP).

    Runs under the project and run locks taken by the caller. A claim whose
    discussion turn is live is left as it is (``release_manual_case``
    refuses it); the run has no runner any more, so it no longer holds one.
    """
    pivot_ids = list(
        TestRunTestCase.objects.filter(
            test_run_id=test_run.pk, status=TestRunTestCaseStatus.IN_PROGRESS
        )
        .exclude(manual_claim_token="")
        .exclude(manual_claim_source=MANUAL_SOURCE_MCP)
        .values_list("id", flat=True)
    )
    for pivot_id in pivot_ids:
        try:
            with transaction.atomic():
                release_manual_case(
                    project=test_run.project,
                    pivot_id=pivot_id,
                    actor=PersonStepActor.web(actor),
                    claim_token=None,
                )
        except ManualRunError:
            continue
