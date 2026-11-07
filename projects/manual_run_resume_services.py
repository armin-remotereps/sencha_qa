"""Resume a Manual run's Cancelled cases: back to the list of cases to test.

A manual case is never continued mid-way: the runner window's draft belongs
to the claim that the abort cleared. Resume puts the run's Cancelled cases
back to Created (checkpoint and claim cleared) and the run back to Running;
a tester claims each case again at the Test Runner and starts from step 1.
No Celery work, no agent and no credit (the hand-in credits the tester).

Must not import ``projects.run_resume_services``, which routes manual runs
here.
"""

from __future__ import annotations

from typing import Final

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.exceptions import RunResumeError
from projects.manual_run_policy import (
    MANUAL_MODE_SWITCH_REFUSED,
    MANUAL_NOTHING_RESUMABLE,
    MANUAL_RESUME_NOT_MANUAL,
    crosses_manual_boundary,
    is_manual_run,
)
from projects.manual_submission_supersede import supersede_manual_submissions
from projects.models import (
    MANUAL_CLAIM_CLEARED,
    MANUAL_CLAIM_FIELDS,
    CaseRestartKind,
    TestRun,
    TestRunCaseResume,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_case_order import RUN_CASE_ORDERING
from projects.run_help_mode_policy import resolve_help_mode
from projects.run_reopen_services import (
    REOPENABLE_RUN_STATUSES,
    RESTART_RESET_FIELDS,
    is_valid_help_mode,
    list_reopen_blockers,
    lock_project_and_run,
    reopen_run,
    reset_case_for_restart,
)
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

# The manual case statuses Resume puts back to Created. Blocked is the
# tester's verdict and is redone by Re-run in place instead.
MANUAL_RESUMABLE_STATUSES: Final[tuple[str, ...]] = (TestRunTestCaseStatus.CANCELLED,)

_NOT_FINISHED: Final = "Only a finished or cancelled run can be resumed."
_INVALID_HELP_MODE: Final = "Choose what happens when a case needs help."

_UPDATE_FIELDS: Final[tuple[str, ...]] = (
    *RESTART_RESET_FIELDS,
    "execution_checkpoint",
    *MANUAL_CLAIM_FIELDS,
)


def count_manual_resumable(test_run: TestRun) -> int:
    """The run's cases a Manual Resume puts back to Created (1 query)."""
    return test_run.pivot_entries.filter(status__in=MANUAL_RESUMABLE_STATUSES).count()


def build_manual_resume_confirm_body(count: int, *, at_test_runner: bool = True) -> str:
    """Server-built Resume dialog text from the count only (no user text).

    ``at_test_runner`` False: the same text without the Test Runner (a run
    performed elsewhere).
    """
    where = "the Test Runner's list" if at_test_runner else "the list of cases to test"
    if count == 1:
        head = (
            f"1 cancelled case goes back to {where}; a tester "
            "starts it again from step 1. The run is Running until it is "
            "handed in"
        )
    else:
        head = (
            f"{count} cancelled cases go back to {where}; a "
            "tester starts each one again from step 1. The run is Running "
            "until they are handed in"
        )
    return (
        f"{head} or the run is stopped. Results stay on this run; Checked "
        "marks on these cases are cleared."
    )


def can_resume_manual_case(pivot: TestRunTestCase) -> bool:
    """Whether a loaded case (with its run) of a Manual run is resumable.

    No query.
    """
    return (
        pivot.status in MANUAL_RESUMABLE_STATUSES
        and pivot.test_run.status in REOPENABLE_RUN_STATUSES
        and is_manual_run(pivot.test_run)
    )


def resume_manual_cases(
    *,
    test_run_id: int,
    pivot_id: int | None,
    user: CustomUser,
    help_mode: str | None,
) -> int:
    """Put the Manual run's Cancelled cases (or ``pivot_id``) back to Created.

    The run goes back to Running with a new generation; the runner window is
    nudged on commit. ``help_mode`` may only keep the mode (Manual): a
    non-superuser changing it gets ``HelpModeNotAllowedError``, a superuser
    leaving Manual ``RunResumeError``. Returns the number of cases resumed.
    """
    if help_mode is not None and not is_valid_help_mode(help_mode):
        raise RunResumeError(_INVALID_HELP_MODE)
    with transaction.atomic():
        project, test_run = lock_project_and_run(test_run_id)
        # The caller routed on an unlocked read; re-check under the lock.
        if not is_manual_run(test_run):
            raise RunResumeError(MANUAL_RESUME_NOT_MANUAL)
        if test_run.status not in REOPENABLE_RUN_STATUSES:
            raise RunResumeError(_NOT_FINISHED)
        resolved_help_mode = resolve_help_mode(
            user=user, requested=help_mode, current=test_run.help_mode
        )
        if resolved_help_mode is not None and crosses_manual_boundary(
            test_run.help_mode, resolved_help_mode
        ):
            raise RunResumeError(MANUAL_MODE_SWITCH_REFUSED)
        blockers = list_reopen_blockers(test_run, project)
        if blockers:
            raise RunResumeError(blockers[0])

        queryset = TestRunTestCase.objects.filter(
            test_run_id=test_run.pk, status__in=MANUAL_RESUMABLE_STATUSES
        )
        if pivot_id is not None:
            queryset = queryset.filter(pk=pivot_id)
        # Logs can be large and are never touched here.
        pivots = list(queryset.defer("logs").order_by(*RUN_CASE_ORDERING))
        if not pivots:
            raise RunResumeError(MANUAL_NOTHING_RESUMABLE)
        pivot_ids = [pivot.pk for pivot in pivots]

        # Before any write: a verdict-discarding hook freezes other sessions.
        # No credit here: the hand-in credits the tester, as at manual Start.
        record_case_activity(
            user=user, pivot_ids=pivot_ids, activity=CaseActivity.RESUME
        )
        TestRunCaseResume.objects.bulk_create(
            [_snapshot(pivot, user) for pivot in pivots]
        )
        now = timezone.now()
        for pivot in pivots:
            reset_case_for_restart(pivot, now)
            # The next claim rebuilds the manual plan from the case's steps.
            pivot.execution_checkpoint = {}
            # Already cleared by the abort; kept defensive.
            for field, value in MANUAL_CLAIM_CLEARED.items():
                setattr(pivot, field, value)
        # Logs and screenshots are kept: results stay on this run.
        TestRunTestCase.objects.bulk_update(pivots, list(_UPDATE_FIELDS))
        # After the case UPDATE (case, then hand-in, as an upload locks): a
        # Created manual case has no live hand-in. A no-op today, since a
        # Cancelled case never has one.
        supersede_manual_submissions(pivot_ids=pivot_ids)
        # The mode stays Manual; a manual reopen queues no chain.
        reopen_run(test_run, pivots, help_mode=None)
    return len(pivots)


def _snapshot(pivot: TestRunTestCase, user: CustomUser) -> TestRunCaseResume:
    return TestRunCaseResume(
        test_run_test_case=pivot,
        resumed_by=user,
        kind=CaseRestartKind.RESUME,
        from_step=None,
        previous_status=pivot.status,
        previous_blocked_reason=pivot.blocked_reason,
        previous_result=pivot.result,
        previous_finished_at=pivot.finished_at,
    )
