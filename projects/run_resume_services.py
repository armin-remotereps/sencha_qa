"""Resume a finished run's Blocked and Cancelled cases in place.

A resumed case keeps its log, screenshots and execution checkpoint; the worker
continues the persisted plan at the checkpoint's step (after a state-restore
sub-task), or plans from scratch when there is no checkpoint.

"Started" for a Cancelled case means it has a non-empty
``execution_checkpoint``: that, not ``started_at``, decides whether the worker
continues or plans from scratch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import CustomUser
from agents.checkpoint import (
    checkpoint_to_json,
    clear_help_continue_pending,
    read_checkpoint_help,
    read_checkpoint_person_step,
    truncate_checkpoint,
    without_person_step,
)
from projects.case_checkpoint_services import load_resume_checkpoint
from projects.exceptions import RunResumeError
from projects.models import (
    Project,
    TestRun,
    TestRunCaseResume,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_case_order import RUN_CASE_ORDERING
from projects.run_reopen_services import (
    REOPENABLE_RUN_STATUSES,
    RESTART_RESET_FIELDS,
    is_valid_help_mode,
    list_reopen_blockers,
    lock_project_and_run,
    reopen_run,
    reset_case_for_restart,
)

RESUMABLE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.BLOCKED,
    TestRunTestCaseStatus.CANCELLED,
)

_NOT_FINISHED = "Only a finished or cancelled run can be resumed."
_NOTHING_RESUMABLE = "There are no Blocked or Cancelled cases to resume."
_INVALID_HELP_MODE = "Choose what happens when a case needs help."


@dataclass(frozen=True)
class ResumePanel:
    """What the run page shows for "Resume run"; texts are user-safe."""

    blocked: int
    cancelled_started: int
    cancelled_unstarted: int
    can_resume: bool
    blockers: tuple[str, ...]
    confirm_body: str


def get_resume_panel(test_run: TestRun, *, blockers: Sequence[str]) -> ResumePanel:
    """Counts, gates and dialog text for "Resume run" (1 query).

    One aggregate over the run's cases. ``blockers`` are the run's
    `list_reopen_blockers` (computed once per page, shared with Re-run).

    With resumable cases but a blocker, ``can_resume`` is False and
    ``blockers`` says why (the page shows the button disabled).
    """
    if test_run.status not in REOPENABLE_RUN_STATUSES:
        return ResumePanel(0, 0, 0, False, (), "")
    no_checkpoint = Q(execution_checkpoint={})
    cancelled = Q(status=TestRunTestCaseStatus.CANCELLED)
    counts = test_run.pivot_entries.aggregate(
        blocked=Count("id", filter=Q(status=TestRunTestCaseStatus.BLOCKED)),
        cancelled_started=Count("id", filter=cancelled & ~no_checkpoint),
        cancelled_unstarted=Count("id", filter=cancelled & no_checkpoint),
    )
    blocked: int = counts["blocked"]
    cancelled_started: int = counts["cancelled_started"]
    cancelled_unstarted: int = counts["cancelled_unstarted"]
    if not blocked + cancelled_started + cancelled_unstarted:
        return ResumePanel(0, 0, 0, False, (), "")

    return ResumePanel(
        blocked=blocked,
        cancelled_started=cancelled_started,
        cancelled_unstarted=cancelled_unstarted,
        can_resume=not blockers,
        blockers=tuple(blockers),
        confirm_body=_build_confirm_body(
            blocked, cancelled_started + cancelled_unstarted
        ),
    )


def can_resume_case(pivot: TestRunTestCase) -> bool:
    """Whether a loaded pivot (with its run) is resumable (no extra query)."""
    return (
        pivot.status in RESUMABLE_STATUSES
        and pivot.test_run.status in REOPENABLE_RUN_STATUSES
    )


def get_run_case_for_resume(
    pivot_id: int, test_run_id: int, project: Project
) -> TestRunTestCase | None:
    """The run's case with its run, for the Resume case POST (1 query, no log)."""
    return (
        TestRunTestCase.objects.select_related("test_run")
        .defer("logs")
        .filter(pk=pivot_id, test_run_id=test_run_id, test_run__project=project)
        .first()
    )


def list_case_resumes(pivot: TestRunTestCase) -> list[TestRunCaseResume]:
    """The case's resume history, oldest first, with who resumed it (1 query)."""
    return list(pivot.resumes.select_related("resumed_by"))


def resume_test_run(
    *, test_run: TestRun, user: CustomUser, help_mode: str | None = None
) -> int:
    """Resume every Blocked and Cancelled case of the run; returns the count.

    ``help_mode`` (a ``RunHelpMode`` value) changes the run's mode; ``None``
    keeps it.
    """
    return _resume_pivots(
        test_run_id=test_run.pk,
        pivot_id=None,
        user=user,
        from_step=None,
        help_mode=help_mode,
    )


def resume_test_run_case(
    *,
    pivot: TestRunTestCase,
    user: CustomUser,
    from_step: int | None,
    help_mode: str | None = None,
) -> None:
    """Resume one case, at ``from_step`` (``None`` = where it stopped).

    ``from_step`` must lie in 1..the checkpoint's cursor (one past the plan
    when every step ran: only the verdict is built); it is ignored when the
    case has no checkpoint. ``help_mode`` as in `resume_test_run`.
    """
    _resume_pivots(
        test_run_id=pivot.test_run_id,
        pivot_id=pivot.pk,
        user=user,
        from_step=from_step,
        help_mode=help_mode,
    )


def _resume_pivots(
    *,
    test_run_id: int,
    pivot_id: int | None,
    user: CustomUser,
    from_step: int | None,
    help_mode: str | None,
) -> int:
    if help_mode is not None and not is_valid_help_mode(help_mode):
        raise RunResumeError(_INVALID_HELP_MODE)
    with transaction.atomic():
        test_run, pivots = _lock_and_check(test_run_id, pivot_id)
        now = timezone.now()
        resumes: list[TestRunCaseResume] = []
        # Logs, screenshots and the checkpoint are kept.
        update_fields = list(RESTART_RESET_FIELDS)
        for pivot in pivots:
            step, truncated = _choose_resume_step(pivot, from_step)
            cleared = _clear_pending_help(pivot)
            if (truncated or cleared) and "execution_checkpoint" not in update_fields:
                update_fields.append("execution_checkpoint")
            resumes.append(_snapshot(pivot, user, step))
            reset_case_for_restart(pivot, now)
        TestRunCaseResume.objects.bulk_create(resumes)
        TestRunTestCase.objects.bulk_update(pivots, update_fields)
        reopen_run(test_run, pivots, help_mode=help_mode)
    return len(pivots)


def _clear_pending_help(pivot: TestRunTestCase) -> bool:
    """Drop a Continue-after-help, or after a person's step, that never ran.

    The run was aborted before a worker picked the case up. A Resume restores
    the machine state and cleans up as usual; only a Continue skips both.
    Returns whether the checkpoint changed (in memory).

    The ``person`` key (Human in the loop: a hand-off, report, overrule or
    hand-back) is dropped whole: the abort cancelled its row, and a report made
    before the restore must not be confirmed after it. The orchestrator hands
    the step out afresh (or runs it, with the mode off) after the restore.
    """
    changed = False
    checkpoint_help = read_checkpoint_help(pivot.execution_checkpoint)
    if checkpoint_help is not None and checkpoint_help.continue_pending:
        pivot.execution_checkpoint = clear_help_continue_pending(
            pivot.execution_checkpoint
        )
        changed = True
    if read_checkpoint_person_step(pivot.execution_checkpoint) is not None:
        pivot.execution_checkpoint = without_person_step(pivot.execution_checkpoint)
        changed = True
    return changed


def _lock_and_check(
    test_run_id: int, pivot_id: int | None
) -> tuple[TestRun, list[TestRunTestCase]]:
    """Lock project then run and apply the gates."""
    project, test_run = lock_project_and_run(test_run_id)
    if test_run.status not in REOPENABLE_RUN_STATUSES:
        raise RunResumeError(_NOT_FINISHED)
    blockers = list_reopen_blockers(test_run, project)
    if blockers:
        raise RunResumeError(blockers[0])

    queryset = TestRunTestCase.objects.filter(
        test_run_id=test_run.pk, status__in=RESUMABLE_STATUSES
    )
    if pivot_id is not None:
        queryset = queryset.filter(pk=pivot_id)
    # Logs can be large and are never touched here.
    pivots = list(queryset.defer("logs").order_by(*RUN_CASE_ORDERING))
    if not pivots:
        raise RunResumeError(_NOTHING_RESUMABLE)
    return test_run, pivots


def _choose_resume_step(
    pivot: TestRunTestCase, from_step: int | None
) -> tuple[int | None, bool]:
    """The step the pivot resumes at, and whether its checkpoint was truncated.

    Truncation is applied to ``pivot.execution_checkpoint`` in place.
    """
    checkpoint = load_resume_checkpoint(pivot)
    if checkpoint is None:
        return None, False
    if from_step is None:
        return checkpoint.next_step, False
    if not 1 <= from_step <= checkpoint.next_step:
        raise RunResumeError(f"Choose a step between 1 and {checkpoint.next_step}.")
    if from_step == checkpoint.next_step:
        return from_step, False
    pivot.execution_checkpoint = checkpoint_to_json(
        truncate_checkpoint(checkpoint, from_step)
    )
    return from_step, True


def _snapshot(
    pivot: TestRunTestCase, user: CustomUser, from_step: int | None
) -> TestRunCaseResume:
    return TestRunCaseResume(
        test_run_test_case=pivot,
        resumed_by=user,
        from_step=from_step,
        previous_status=pivot.status,
        previous_blocked_reason=pivot.blocked_reason,
        previous_result=pivot.result,
        previous_finished_at=pivot.finished_at,
    )


def _build_confirm_body(blocked: int, cancelled: int) -> str:
    """Server-built dialog text from counts only (no user or model text)."""
    parts: list[str] = []
    if blocked:
        parts.append(
            f"{_count(blocked, 'blocked case')} {_verb(blocked, 'continue')} "
            "from the step where they stopped (the agent first restores the "
            "machine state)"
        )
    if cancelled:
        parts.append(
            f"{_count(cancelled, 'cancelled case')} {_verb(cancelled, 'continue')} "
            f"or {_verb(cancelled, 'start')}"
        )
    return (
        "; ".join(parts)
        + ". Results stay on this run; Checked marks on these cases are cleared."
    )


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _verb(number: int, verb: str) -> str:
    return f"{verb}s" if number == 1 else verb
