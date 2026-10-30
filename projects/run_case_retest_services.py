"""Mark run cases Needs re-test (any non-draft run, even mid-run), and undo it.

A person flags a result as untrustworthy. The mark keeps the earlier verdict
and its evidence (`case_retest_marking.apply_retest_mark`); Re-run or Reset
clears it, Undo restores the earlier verdict.

Marking has two phases:

1. One transaction for every selected case that is not waiting (finished,
   not started or running). A running case's worker sees the mark at its next
   cancellation check and stops without a verdict; a not-started case's task
   skips it at pick-up.
2. The run's waiting case (Need help or Your turn; at most one per run), if
   selected, in its own transaction through the help or person step
   resolution, which closes the request or step and lets the run go on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from notifications.services.inbox_broadcast import broadcast_project_attention_changed
from projects.case_duration_services import close_case_segment
from projects.case_help_services import (
    HelpActionOutcome,
    continue_paused_run,
    mark_retest_after_help,
)
from projects.case_result_change_services import supersede_pending_result_changes
from projects.case_retest_marking import RETEST_MARK_FIELDS, apply_retest_mark
from projects.case_review_redesign_store import supersede_cases_review_drafts
from projects.exceptions import PersonStepError, RunCaseRetestError, RunHelpError
from projects.models import (
    OPEN_PERSON_STEP_STATUSES,
    RETEST_CLEARED,
    RETEST_VERDICT_STATUSES,
    CasePersonStep,
    PersonStepClose,
    PersonStepStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_actor import PersonStepActor
from projects.person_step_services import mark_retest_person_step
from projects.run_reopen_services import (
    list_testrail_push_blockers,
    lock_project_and_run,
)
from projects.services import broadcast_run_and_pivots
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

_DRAFT_RUN = "A draft run has nothing to re-test yet."
_NOTHING_SELECTED = "None of the selected cases can be marked for re-test."
_NOT_MARKED = "This case is not marked for re-test."
_NO_EARLIER_RESULT = (
    "This case had no result before it was marked; re-run it to clear the mark."
)
_WAITING_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.NEED_HELP,
    TestRunTestCaseStatus.AWAITING_PERSON,
)


@dataclass(frozen=True)
class RetestOutcome:
    """How many selected cases were marked.

    ``problem``: a user-safe refusal of the waiting case's resolution (the
    other marks stand). ``run_notice``: user-safe text when the run could not
    go on yet after the waiting case was closed.
    """

    marked: int
    skipped: int
    problem: str = ""
    run_notice: str = ""


@dataclass(frozen=True)
class RetestPanel:
    """The case page's Needs re-test card; all texts are user-safe."""

    marked_by: str
    marked_at_iso: str
    from_label: str
    note: str
    can_undo: bool
    undo_blocker: str


def is_retest_open(test_run: TestRun) -> bool:
    """Whether cases of the run can be marked (any status but Draft)."""
    return test_run.status != TestRunStatus.WAITING


def is_case_retestable(test_run: TestRun, pivot: TestRunTestCase) -> bool:
    """Whether an already-loaded case can be marked now (no query)."""
    return (
        is_retest_open(test_run) and pivot.status != TestRunTestCaseStatus.NEEDS_RETEST
    )


def retestable_ids_on_page(
    test_run: TestRun, pivots: Sequence[TestRunTestCase]
) -> list[int]:
    """Ids of the page's loaded cases that can be marked (no query)."""
    return [pivot.pk for pivot in pivots if is_case_retestable(test_run, pivot)]


def list_retestable_pivot_ids(test_run: TestRun) -> list[int]:
    """Ids of the run's cases that can be marked, for form choices (1 query)."""
    if not is_retest_open(test_run):
        return []
    return list(
        test_run.pivot_entries.exclude(
            status=TestRunTestCaseStatus.NEEDS_RETEST
        ).values_list("id", flat=True)
    )


def mark_cases_for_retest(
    *,
    test_run: TestRun,
    pivot_ids: Sequence[int],
    user: CustomUser,
    note: str,
) -> RetestOutcome:
    """Mark the selected cases of the run Needs re-test.

    Raises ``RunCaseRetestError`` (user-safe) for a Draft run, when no
    selected case can be marked, or while a TestRail send or push of the run
    is live and a selected case has a verdict. The waiting case's resolution
    runs after the other marks commit; its refusal is returned as
    ``problem``, not raised.
    """
    requested = set(pivot_ids)
    waiting: TestRunTestCase | None = None
    marked = 0
    with transaction.atomic():
        project, locked_run = lock_project_and_run(test_run.pk)
        if not is_retest_open(locked_run):
            raise RunCaseRetestError(_DRAFT_RUN)
        statuses = dict(
            TestRunTestCase.objects.filter(test_run_id=locked_run.pk, pk__in=requested)
            .exclude(status=TestRunTestCaseStatus.NEEDS_RETEST)
            .values_list("id", "status")
        )
        if not statuses:
            raise RunCaseRetestError(_NOTHING_SELECTED)
        if any(status in RETEST_VERDICT_STATUSES for status in statuses.values()):
            blockers = list_testrail_push_blockers(locked_run)
            if blockers:
                raise RunCaseRetestError(blockers[0])
        direct_ids = [
            pid for pid, status in statuses.items() if status not in _WAITING_STATUSES
        ]
        waiting_ids = [
            pid for pid, status in statuses.items() if status in _WAITING_STATUSES
        ]
        if direct_ids:
            marked = _mark_direct(locked_run, direct_ids, user, note)
            if marked:
                project_id = project.pk
                transaction.on_commit(
                    lambda: broadcast_project_attention_changed(project_id)
                )
        if waiting_ids:
            waiting = (
                TestRunTestCase.objects.defer("logs")
                .select_related("test_run")
                .get(pk=waiting_ids[0])
            )
        wake_paused = (
            not waiting_ids
            and marked > 0
            and locked_run.status == TestRunStatus.PAUSED
            and not TestRunTestCase.objects.filter(
                test_run_id=locked_run.pk, status__in=_WAITING_STATUSES
            ).exists()
        )

    problem = ""
    run_notice = ""
    if waiting is not None:
        try:
            run_notice = _mark_waiting(waiting, user, note)
            marked += 1
        except (RunHelpError, PersonStepError) as exc:
            problem = exc.message
    elif wake_paused:
        # The last pending cases of a paused run whose help is already
        # resolved: go on (or finish) as "Continue run" does. A refusal keeps
        # the marks and leaves "Continue run" on the page.
        try:
            continue_paused_run(test_run=locked_run, user=user)
        except RunHelpError:
            pass
    skipped = len(requested) - marked - (1 if problem else 0)
    return RetestOutcome(
        marked=marked,
        skipped=max(skipped, 0),
        problem=problem,
        run_notice=run_notice,
    )


def _mark_direct(
    test_run: TestRun, pivot_ids: list[int], user: CustomUser, note: str
) -> int:
    """Phase 1 under the project and run locks; returns the number marked."""
    # A Human in the loop case being confirmed is IN_PROGRESS with a reported
    # step: lock its open step before the case (the order help actions use).
    person_step_ids = list(
        CasePersonStep.objects.select_for_update()
        .filter(
            test_run_test_case_id__in=pivot_ids,
            status__in=OPEN_PERSON_STEP_STATUSES,
        )
        .order_by("pk")
        .values_list("id", flat=True)
    )
    pivots = list(
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(test_run_id=test_run.pk, pk__in=pivot_ids)
        .exclude(status__in=(TestRunTestCaseStatus.NEEDS_RETEST, *_WAITING_STATUSES))
        .order_by("pk")
    )
    if not pivots:
        return 0
    marked_ids = [pivot.pk for pivot in pivots]
    # Before the write: other sessions keep the verdict they saw.
    record_case_activity(user=user, pivot_ids=marked_ids, activity=CaseActivity.RETEST)
    now = timezone.now()
    for pivot in pivots:
        if pivot.status == TestRunTestCaseStatus.IN_PROGRESS:
            close_case_segment(pivot.pk, now)
        apply_retest_mark(pivot, user=user, note=note, now=now)
    TestRunTestCase.objects.bulk_update(pivots, RETEST_MARK_FIELDS)
    if person_step_ids:
        CasePersonStep.objects.filter(pk__in=person_step_ids).update(
            status=PersonStepStatus.CANCELLED,
            close_reason=PersonStepClose.MARKED_RETEST,
            closed_by=user,
            closed_at=now,
        )
    supersede_pending_result_changes(marked_ids)
    supersede_cases_review_drafts(marked_ids)
    transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, pivots))
    return len(pivots)


def _mark_waiting(pivot: TestRunTestCase, user: CustomUser, note: str) -> str:
    """Phase 2: close the waiting case's help request or person step.

    Returns user-safe text when the run cannot go on now, else "".
    """
    if pivot.status == TestRunTestCaseStatus.NEED_HELP:
        help_result = mark_retest_after_help(pivot=pivot, user=user, note=note)
        if help_result.outcome == HelpActionOutcome.WAITING_FOR_AUTO_FIX:
            return "The run continues as soon as the auto-fix agent stops."
        if help_result.outcome == HelpActionOutcome.WAITING_FOR_GATES:
            return _gates_notice(help_result.blocker)
        return ""
    step_result = mark_retest_person_step(
        pivot=pivot, actor=PersonStepActor.web(user), note=note
    )
    return "" if step_result.queued else _gates_notice(step_result.blocker)


def _gates_notice(blocker: str) -> str:
    return (
        f"The run cannot continue yet: {blocker} "
        "Then use Continue run on the run page."
    )


def undo_retest(*, pivot: TestRunTestCase, user: CustomUser) -> None:
    """Restore the verdict the case had before it was marked.

    Refused (``RunCaseRetestError``) unless the case is Needs re-test from a
    Passed, Failed or Blocked verdict, or while a TestRail send or push of the
    run is live. ``finished_at`` is kept; Checked/Sent stay cleared.
    """
    with transaction.atomic():
        _, locked_run = lock_project_and_run(pivot.test_run_id)
        locked = (
            TestRunTestCase.objects.select_for_update()
            .defer("logs")
            .filter(pk=pivot.pk, test_run_id=locked_run.pk)
            .first()
        )
        if locked is None or locked.status != TestRunTestCaseStatus.NEEDS_RETEST:
            raise RunCaseRetestError(_NOT_MARKED)
        if locked.retest_from_status not in RETEST_VERDICT_STATUSES:
            raise RunCaseRetestError(_NO_EARLIER_RESULT)
        blockers = list_testrail_push_blockers(locked_run)
        if blockers:
            raise RunCaseRetestError(blockers[0])
        now = timezone.now()
        TestRunTestCase.objects.filter(pk=locked.pk).update(
            status=locked.retest_from_status, updated_at=now, **RETEST_CLEARED
        )
        locked.refresh_from_db(fields=["status", *RETEST_CLEARED, "updated_at"])
        transaction.on_commit(lambda: broadcast_run_and_pivots(locked_run, [locked]))


def get_case_retest_panel(
    test_run: TestRun, pivot: TestRunTestCase
) -> RetestPanel | None:
    """The case page's Needs re-test card, or None when it is not marked.

    ``pivot.retest_marked_by`` should be loaded with the case.
    """
    if pivot.status != TestRunTestCaseStatus.NEEDS_RETEST:
        return None
    marker = pivot.retest_marked_by
    undo_blocker = ""
    if pivot.retest_from_status not in RETEST_VERDICT_STATUSES:
        undo_blocker = _NO_EARLIER_RESULT
    else:
        blockers = list_testrail_push_blockers(test_run)
        if blockers:
            undo_blocker = blockers[0]
    return RetestPanel(
        marked_by=(marker.get_full_name() or marker.email) if marker else "",
        marked_at_iso=(
            pivot.retest_marked_at.isoformat() if pivot.retest_marked_at else ""
        ),
        from_label=_status_label(pivot.retest_from_status),
        note=pivot.retest_note,
        can_undo=not undo_blocker,
        undo_blocker=undo_blocker,
    )


def format_retest_message(outcome: RetestOutcome) -> str:
    """Flash text, e.g. "Marked 3 cases for re-test · 1 skipped"."""
    noun = "case" if outcome.marked == 1 else "cases"
    message = f"Marked {outcome.marked} {noun} for re-test"
    if outcome.skipped:
        message += f" · {outcome.skipped} skipped"
    if outcome.run_notice:
        message += f". {outcome.run_notice}"
    return message


def _status_label(status: str) -> str:
    if not status:
        return ""
    return str(TestRunTestCaseStatus(status).label)
