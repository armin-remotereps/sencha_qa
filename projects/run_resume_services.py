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
from datetime import datetime

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import CustomUser
from agents.checkpoint import (
    checkpoint_to_json,
    truncate_checkpoint,
)
from projects.case_checkpoint_services import load_resume_checkpoint
from projects.checked_push_limits import is_checked_push_live
from projects.exceptions import RunResumeError
from projects.models import (
    Project,
    TestRailPushStatus,
    TestRun,
    TestRunCaseResume,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_case_order import RUN_CASE_ORDERING
from projects.services import (
    MachineState,
    broadcast_run_and_pivots,
    get_machine_state,
    queue_test_run_cases,
)

RESUMABLE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.BLOCKED,
    TestRunTestCaseStatus.CANCELLED,
)
_RESUMABLE_RUN_STATUSES: tuple[str, ...] = (TestRunStatus.DONE, TestRunStatus.CANCELLED)

_NOT_FINISHED = "Only a finished or cancelled run can be resumed."
_OTHER_RUN_ACTIVE = "Another test run is already in progress for this project."
_CHECKED_PUSH_LIVE = "Wait for the checked results to finish sending to TestRail."
_RESULTS_PUSHING = "Wait for the results to finish pushing to TestRail."
_MACHINE_NOT_READY = "Connect the Test Runner and wait for the visual engine."
_MACHINE_FAILED = (
    "The visual engine on the test machine failed. "
    "Check Advanced diagnostics on the Environment page."
)
_NOTHING_RESUMABLE = "There are no Blocked or Cancelled cases to resume."

# Every execution and Checked/Sent field a resume resets, as `reset_test_run`
# does; logs, screenshots and the checkpoint are kept (started_at is set again
# when the worker picks the case up; the resume row keeps the earlier finish).
_RESET_PIVOT_FIELDS: tuple[str, ...] = (
    "status",
    "blocked_reason",
    "result",
    "finished_at",
    "checked_at",
    "checked_by",
    "testrail_source_result_id",
    "testrail_source_sent_at",
    "testrail_source_attachment_count",
    "testrail_source_skip_reason",
    "updated_at",
)


@dataclass(frozen=True)
class ResumePanel:
    """What the run page shows for "Resume run"; texts are user-safe."""

    blocked: int
    cancelled_started: int
    cancelled_unstarted: int
    can_resume: bool
    blockers: tuple[str, ...]
    confirm_body: str


def get_resume_panel(test_run: TestRun, project: Project) -> ResumePanel:
    """Counts, gates and dialog text for "Resume run" (at most 2 queries).

    One aggregate over the run's cases plus the "another run is STARTED"
    check; the machine state comes from the run's ``project``.

    With resumable cases but a blocker, ``can_resume`` is False and
    ``blockers`` says why (the page shows the button disabled).
    """
    if test_run.status not in _RESUMABLE_RUN_STATUSES:
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

    blockers: list[str] = []
    if _has_other_started_run(test_run.project_id, test_run.pk):
        blockers.append(_OTHER_RUN_ACTIVE)
    blockers.extend(_push_blockers(test_run))
    machine_blocker = _machine_blocker(project)
    if machine_blocker:
        blockers.append(machine_blocker)
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
        and pivot.test_run.status in _RESUMABLE_RUN_STATUSES
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


def resume_test_run(*, test_run: TestRun, user: CustomUser) -> int:
    """Resume every Blocked and Cancelled case of the run; returns the count."""
    return _resume_pivots(
        test_run_id=test_run.pk, pivot_id=None, user=user, from_step=None
    )


def resume_test_run_case(
    *, pivot: TestRunTestCase, user: CustomUser, from_step: int | None
) -> None:
    """Resume one case, at ``from_step`` (``None`` = where it stopped).

    ``from_step`` must lie in 1..the checkpoint's cursor (one past the plan
    when every step ran: only the verdict is built); it is ignored when the
    case has no checkpoint.
    """
    _resume_pivots(
        test_run_id=pivot.test_run_id,
        pivot_id=pivot.pk,
        user=user,
        from_step=from_step,
    )


def _resume_pivots(
    *,
    test_run_id: int,
    pivot_id: int | None,
    user: CustomUser,
    from_step: int | None,
) -> int:
    with transaction.atomic():
        test_run, pivots = _lock_and_check(test_run_id, pivot_id)
        now = timezone.now()
        resumes: list[TestRunCaseResume] = []
        update_fields = list(_RESET_PIVOT_FIELDS)
        for pivot in pivots:
            step, truncated = _choose_resume_step(pivot, from_step)
            if truncated and "execution_checkpoint" not in update_fields:
                update_fields.append("execution_checkpoint")
            resumes.append(_snapshot(pivot, user, step))
            _reset_execution_fields(pivot, now)
        TestRunCaseResume.objects.bulk_create(resumes)
        TestRunTestCase.objects.bulk_update(pivots, update_fields)

        test_run.status = TestRunStatus.STARTED
        test_run.finished_at = None
        # Workers of the aborted chain see the new generation and stand down.
        test_run.execution_generation += 1
        test_run.save(
            update_fields=[
                "status",
                "finished_at",
                "execution_generation",
                "updated_at",
            ]
        )

        pivot_ids = [pivot.pk for pivot in pivots]

        def _enqueue() -> None:
            if queue_test_run_cases(test_run, pivot_ids):
                broadcast_run_and_pivots(test_run, pivots)

        # Workers must see the STARTED run and CREATED cases.
        transaction.on_commit(_enqueue)
    return len(pivots)


def _lock_and_check(
    test_run_id: int, pivot_id: int | None
) -> tuple[TestRun, list[TestRunTestCase]]:
    """Lock project then run (the order `start_test_run` uses) and apply gates."""
    project_id = TestRun.objects.values_list("project_id", flat=True).get(
        pk=test_run_id
    )
    project = Project.objects.select_for_update().get(pk=project_id)
    test_run = TestRun.objects.select_for_update().get(pk=test_run_id)

    if test_run.status not in _RESUMABLE_RUN_STATUSES:
        raise RunResumeError(_NOT_FINISHED)
    if _has_other_started_run(project.pk, test_run.pk):
        raise RunResumeError(_OTHER_RUN_ACTIVE)
    push_blockers = _push_blockers(test_run)
    if push_blockers:
        raise RunResumeError(push_blockers[0])
    machine_blocker = _machine_blocker(project)
    if machine_blocker:
        raise RunResumeError(machine_blocker)

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


def _has_other_started_run(project_id: int, test_run_id: int) -> bool:
    return (
        TestRun.objects.filter(project_id=project_id, status=TestRunStatus.STARTED)
        .exclude(pk=test_run_id)
        .exists()
    )


def _push_blockers(test_run: TestRun) -> list[str]:
    blockers: list[str] = []
    if is_checked_push_live(test_run, timezone.now()):
        blockers.append(_CHECKED_PUSH_LIVE)
    if test_run.testrail_push_status == TestRailPushStatus.PUSHING:
        blockers.append(_RESULTS_PUSHING)
    return blockers


def _machine_blocker(project: Project) -> str | None:
    state = get_machine_state(project)
    if state == MachineState.READY:
        return None
    if state == MachineState.FAILED:
        return _MACHINE_FAILED
    return _MACHINE_NOT_READY


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


def _reset_execution_fields(pivot: TestRunTestCase, now: datetime) -> None:
    pivot.status = TestRunTestCaseStatus.CREATED
    pivot.blocked_reason = ""
    pivot.result = ""
    pivot.finished_at = None
    pivot.checked_at = None
    pivot.checked_by = None
    pivot.testrail_source_result_id = None
    pivot.testrail_source_sent_at = None
    pivot.testrail_source_attachment_count = 0
    pivot.testrail_source_skip_reason = ""
    # bulk_update does not run auto_now.
    pivot.updated_at = now


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
