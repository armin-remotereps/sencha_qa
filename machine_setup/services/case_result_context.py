"""Load a finished case's result-chat brief: shared by the opener and the tools.

``load_result_case`` is one query (case and run joined, the log deferred and
only its length read). ``case_result_brief_data`` adds about six small
indexed queries: help history, earlier attempts, applied and pending result
changes, the pending redesign and the screenshot list.
"""

from __future__ import annotations

import math

from django.db.models.functions import Length

from agents.checkpoint import checkpoint_from_json
from agents.plan_revision import (
    attempt_origin_label,
    carriable_steps,
    review_carriable_note,
    snapshot_plan,
)
from agents.types import OrchestratorCheckpoint, SubTaskResult
from machine_setup.case_result_brief import (
    FAILED_DIGESTS_MAX,
    SCREENSHOT_LIST_MAX,
    AppliedChange,
    CaseResultBriefData,
    EarlierAttempt,
    EarlierPlan,
    FailedDigest,
    HelpEntry,
    PendingChange,
    ResultDecision,
    ResultPlanStep,
    ScreenshotEntry,
    build_case_result_brief,
    build_case_result_update,
)
from projects.case_result_change_services import (
    get_result_change_view,
    is_result_changeable,
)
from projects.case_result_text import person_label, result_status_label
from projects.case_review_redesign_services import (
    get_pending_review_redesign,
    get_review_draft_view,
)
from projects.case_step_refs import format_case_steps_label
from projects.models import (
    CaseHelpRequest,
    CaseResultChange,
    ResultChangeStatus,
    TestRunCaseResume,
    TestRunScreenshot,
    TestRunTestCase,
)

# Characters of one ``read_case_log`` page.
LOG_PAGE_CHARS = 12_000
# Newest rows of each history listed in the brief.
_HELP_HISTORY_LIMIT = 6
_EARLIER_ATTEMPTS_LIMIT = 5
_APPLIED_CHANGES_LIMIT = 5

_STEP_STATUS_LABELS = {
    "passed": "passed",
    "failed": "failed",
    "not_run": "not run",
    "needs_help": "needs help",
}
_SOURCE_LABELS = {"help": "help chat", "review": "result review"}
_TIME_FORMAT = "%Y-%m-%d %H:%M UTC"


def load_result_case(pivot_id: int) -> TestRunTestCase | None:
    """The case with its test case and run; the log deferred; one query.

    The row carries ``log_chars`` (the log's length).
    """
    return (
        TestRunTestCase.objects.select_related("test_case", "test_run")
        .defer("logs")
        .annotate(log_chars=Length("logs"))
        .filter(pk=pivot_id)
        .first()
    )


def log_page_count(log_chars: int) -> int:
    return max(1, math.ceil(log_chars / LOG_PAGE_CHARS))


def build_result_brief(pivot: TestRunTestCase) -> str:
    """The first message of the case's result chat (``pivot`` from ``load_result_case``)."""
    return build_case_result_brief(case_result_brief_data(pivot))


def build_result_update(pivot: TestRunTestCase) -> str:
    """The ``get_case_result`` result (``pivot`` from ``load_result_case``)."""
    return build_case_result_update(case_result_brief_data(pivot))


def describe_result_status(pivot_id: int) -> str:
    """One line for the result chat's system prompt; one query."""
    pivot = (
        TestRunTestCase.objects.only("id", "status", "test_run_id")
        .filter(pk=pivot_id)
        .first()
    )
    if pivot is None:
        return (
            "The test case of this chat no longer exists. The result tools "
            "refuse; this chat continues as an ordinary machine setup chat."
        )
    return _status_text(pivot)


def case_result_brief_data(pivot: TestRunTestCase) -> CaseResultBriefData:
    case = pivot.test_case
    test_run = pivot.test_run
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    log_chars = int(getattr(pivot, "log_chars", 0) or 0)
    pending_draft = None
    pending_draft_outdated = False
    revision = get_pending_review_redesign(pivot)
    if revision is not None:
        review = get_review_draft_view(revision, pivot)
        pending_draft = review.draft
        pending_draft_outdated = review.outdated
    screenshots, screenshot_total = _screenshots(pivot.pk)
    return CaseResultBriefData(
        run_name=test_run.name,
        run_status_label=test_run.get_status_display(),
        case_title=case.title,
        preconditions=case.preconditions,
        steps=case.steps,
        expected=case.expected,
        status_label=result_status_label(pivot.status),
        blocked_reason_label=(
            pivot.get_blocked_reason_display() if pivot.blocked_reason else ""
        ),
        result=pivot.result,
        finished_at=(
            pivot.finished_at.strftime(_TIME_FORMAT) if pivot.finished_at else ""
        ),
        status_text=_status_text(pivot),
        plan=_plan(checkpoint),
        decisions=(
            tuple(
                ResultDecision(
                    step_num=decision.step_num,
                    action=decision.action,
                    reason=decision.reason,
                )
                for decision in checkpoint.decisions
            )
            if checkpoint is not None
            else ()
        ),
        carriable_note=(
            review_carriable_note(checkpoint)
            if checkpoint is not None and checkpoint.plan
            else ""
        ),
        failed_digests=_failed_digests(checkpoint),
        help_history=_help_history(pivot.pk),
        earlier_attempts=_earlier_attempts(pivot.pk),
        applied_changes=_applied_changes(pivot.pk),
        earlier_plans=_earlier_plans(checkpoint),
        pending_change=_pending_change(pivot),
        pending_draft=pending_draft,
        pending_draft_outdated=pending_draft_outdated,
        screenshots=screenshots,
        screenshot_total=screenshot_total,
        log_chars=log_chars,
        log_pages=log_page_count(log_chars),
    )


def _status_text(pivot: TestRunTestCase) -> str:
    label = result_status_label(pivot.status)
    if is_result_changeable(pivot):
        return f"The case is {label}; result changes and redesigns can be proposed."
    return (
        f"The case is {label} now (it is running again or no longer has a "
        "result); proposals are refused until it has a result again."
    )


def _plan(checkpoint: OrchestratorCheckpoint | None) -> tuple[ResultPlanStep, ...]:
    if checkpoint is None:
        return ()
    carriable = carriable_steps(checkpoint)
    finals: dict[int, SubTaskResult] = {}
    carried_labels: dict[int, str] = {}
    for attempt in checkpoint.attempts:
        if attempt.kind != "step":
            continue
        finals[attempt.step_num] = attempt.result
        carried_labels[attempt.step_num] = attempt_origin_label(
            attempt, checkpoint.plan_revisions
        )
    steps = []
    # ``snapshot_plan`` has one snapshot per plan step, in order.
    for num, (snapshot, task) in enumerate(
        zip(snapshot_plan(checkpoint), checkpoint.plan, strict=True), start=1
    ):
        final = finals.get(num)
        steps.append(
            ResultPlanStep(
                description=snapshot.description,
                expected_result=snapshot.expected_result,
                status_label=_STEP_STATUS_LABELS[snapshot.status],
                summary=final.summary if final is not None else "",
                carried_label=carried_labels.get(num, ""),
                carriable=num in carriable,
                case_steps_label=format_case_steps_label(task.case_steps),
            )
        )
    return tuple(steps)


def _failed_digests(
    checkpoint: OrchestratorCheckpoint | None,
) -> tuple[FailedDigest, ...]:
    if checkpoint is None:
        return ()
    digests: list[FailedDigest] = []
    for attempt in reversed(checkpoint.attempts):
        if attempt.result.status == "fail" and attempt.result.actions_digest.strip():
            digests.append(
                FailedDigest(
                    step_num=attempt.step_num,
                    kind=attempt.kind,
                    digest=attempt.result.actions_digest,
                )
            )
            if len(digests) >= FAILED_DIGESTS_MAX:
                break
    return tuple(digests)


def _earlier_plans(
    checkpoint: OrchestratorCheckpoint | None,
) -> tuple[EarlierPlan, ...]:
    if checkpoint is None:
        return ()
    return tuple(
        EarlierPlan(
            version=record.version,
            source_label=_SOURCE_LABELS.get(record.source, record.source),
            reason=record.reason,
            previous_steps=tuple(
                description for description, _ in record.previous_plan
            ),
            mapping=record.mapping,
            resume_step=record.resume_step,
        )
        for record in checkpoint.plan_revisions
    )


def _help_history(pivot_id: int) -> tuple[HelpEntry, ...]:
    rows = list(
        CaseHelpRequest.objects.filter(test_run_test_case_id=pivot_id)
        .only(
            "id",
            "step_num",
            "reason",
            "ask",
            "resolution",
            "note",
            "auto_fix_summary",
            "requested_at",
        )
        .order_by("-requested_at", "-id")[:_HELP_HISTORY_LIMIT]
    )
    rows.reverse()
    return tuple(
        HelpEntry(
            step_num=row.step_num,
            reason=row.reason,
            ask=row.ask,
            resolution_label=row.get_resolution_display(),
            note=row.note,
            auto_fix_summary=row.auto_fix_summary,
        )
        for row in rows
    )


def _earlier_attempts(pivot_id: int) -> tuple[EarlierAttempt, ...]:
    rows = list(
        TestRunCaseResume.objects.filter(test_run_test_case_id=pivot_id)
        .select_related("resumed_by")
        .order_by("-created_at", "-id")[:_EARLIER_ATTEMPTS_LIMIT]
    )
    rows.reverse()
    return tuple(
        EarlierAttempt(
            kind_label=row.get_kind_display(),
            created_at=row.created_at.strftime(_TIME_FORMAT),
            by_name=person_label(row.resumed_by),
            from_step=row.from_step,
            previous_status_label=result_status_label(row.previous_status),
            previous_result=row.previous_result,
        )
        for row in rows
    )


def _applied_changes(pivot_id: int) -> tuple[AppliedChange, ...]:
    rows = list(
        CaseResultChange.objects.filter(
            test_run_test_case_id=pivot_id, status=ResultChangeStatus.APPLIED
        )
        .select_related("decided_by")
        .only(
            "id",
            "from_status",
            "to_status",
            "summary",
            "decided_at",
            "proposed_at",
            "decided_by__id",
            "decided_by__first_name",
            "decided_by__last_name",
            "decided_by__email",
        )
        .order_by("-decided_at", "-id")[:_APPLIED_CHANGES_LIMIT]
    )
    rows.reverse()
    return tuple(
        AppliedChange(
            from_label=result_status_label(row.from_status),
            to_label=result_status_label(row.to_status),
            decided_by_name=person_label(row.decided_by),
            decided_at=(
                row.decided_at.strftime(_TIME_FORMAT) if row.decided_at else ""
            ),
            summary=row.summary,
        )
        for row in rows
    )


def _pending_change(pivot: TestRunTestCase) -> PendingChange | None:
    change = (
        CaseResultChange.objects.filter(
            test_run_test_case_id=pivot.pk, status=ResultChangeStatus.PROPOSED
        )
        # A proposed change has no decider (``decided_by`` stays unloaded).
        .first()
    )
    if change is None:
        return None
    view = get_result_change_view(change, pivot)
    return PendingChange(
        from_label=view.from_label,
        to_label=view.to_label,
        summary=view.summary,
        explanation=view.explanation,
        outdated=view.outdated,
    )


def _screenshots(pivot_id: int) -> tuple[tuple[ScreenshotEntry, ...], int]:
    """The newest ``SCREENSHOT_LIST_MAX`` (oldest first) and the total; two queries."""
    queryset = TestRunScreenshot.objects.filter(test_run_test_case_id=pivot_id)
    rows = list(
        queryset.order_by("-created_at", "-id").values("id", "created_at", "tool_name")[
            :SCREENSHOT_LIST_MAX
        ]
    )
    total = len(rows) if len(rows) < SCREENSHOT_LIST_MAX else queryset.count()
    rows.reverse()
    return (
        tuple(
            ScreenshotEntry(
                screenshot_id=row["id"],
                created_at=row["created_at"].strftime(_TIME_FORMAT),
                tool_name=row["tool_name"],
            )
            for row in rows
        ),
        total,
    )
