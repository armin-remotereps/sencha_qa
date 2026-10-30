"""Send checked run cases' results, with screenshots, to their source TestRail run.

The source run is the TestRail run a case was imported from with a run link,
snapshotted on each pivot when it joins a Punk Hazard run. Results are added
once per execution; the stored result id and attachment counter make a failed
send resumable without duplicates.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import groupby
from typing import Any, TypedDict

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import (
    BooleanField,
    Count,
    ExpressionWrapper,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import CustomUser
from auto_tester.celery import app as celery_app
from projects.checked_push_limits import (
    CHECKED_PUSH_STALE_AFTER,
    is_checked_push_live,
    is_checked_push_stale,
)
from projects.exceptions import (
    CheckedPushError,
    CheckedPushInProgressError,
    NothingToSendError,
    RunNotFinishedError,
)
from projects.models import (
    CheckedPushSkipReason,
    Project,
    TestRailPushStatus,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
)
from projects.run_case_order import RUN_CASE_ORDERING
from projects.services import _test_run_group
from projects.testrail_client import (
    TestRailClient,
    TestRailError,
    TestRailResult,
    TestRailResultInput,
    TestRailRun,
)
from projects.testrail_evidence import OVERLAY_TOOL_NAME
from projects.testrail_mapping import (
    build_checked_result_comment,
    format_checked_push_confirm_body,
    format_checked_push_summary,
    format_testrail_elapsed,
    map_pivot_status_to_testrail,
    parse_testrail_case_id,
    screenshot_attachment_filename,
    testrail_outcome_label,
)
from projects.testrail_results_services import (
    build_testrail_run_url,
    pivot_result_text,
    pivot_step_breakdown,
)
from projects.testrail_services import (
    TestRailNotConfiguredError,
    build_testrail_client,
)

logger = logging.getLogger(__name__)

CHECKED_PUSH_BATCH_SIZE = 100
CHECKED_PUSH_TASK = "projects.tasks.push_checked_results_to_testrail"
CHECKED_PUSH_TIMEOUT_MESSAGE = "Timed out; send again to resume."
CHECKED_PUSH_INTERRUPTED_MESSAGE = (
    "The previous send was interrupted; send again to resume."
)

_FINISHED_STATUSES = (TestRunStatus.DONE, TestRunStatus.CANCELLED)
TESTRAIL_NOT_CONFIGURED_MESSAGE = (
    "TestRail is not configured for this project. "
    "Add the URL, email and API key under Settings."
)
_RUN_NOT_FINISHED_MESSAGE = "Only finished runs can send checked results to TestRail."
_DISPATCH_FAILED_MESSAGE = (
    "Could not start sending to TestRail: the background worker is unreachable."
)
_UNKNOWN_REVIEWER = "a deleted user"


# ============================================================================
# SHARED QUERY PIECES
# ============================================================================


def _evidence_screenshots() -> QuerySet[TestRunScreenshot]:
    """The screenshots that are attached to TestRail, in upload order.

    Only the evidence frozen when the case was checked
    (`testrail_evidence`, see `testrail_evidence_services`); overlays stay
    excluded as a belt. The flags only change at check, which is refused once
    sent, and `(created_at, id)` is stable across attempts, which is what makes
    the per-pivot attachment counter a valid resume index.
    """
    return (
        TestRunScreenshot.objects.filter(testrail_evidence=True)
        .exclude(tool_name=OVERLAY_TOOL_NAME)
        .order_by("created_at", "id")
    )


def _with_planned_shot_count(
    pivots: QuerySet[TestRunTestCase],
) -> QuerySet[TestRunTestCase]:
    """Annotate `planned_shots`: how many screenshots each pivot will attach."""
    shot_count = (
        TestRunScreenshot.objects.filter(
            test_run_test_case=OuterRef("pk"), testrail_evidence=True
        )
        .exclude(tool_name=OVERLAY_TOOL_NAME)
        .order_by()
        .values("test_run_test_case")
        .annotate(total=Count("id"))
        .values("total")
    )
    return pivots.annotate(
        planned_shots=Coalesce(
            Subquery(shot_count, output_field=IntegerField()), Value(0)
        )
    )


_CHECKED_Q = Q(checked_at__isnull=False)
_SOURCED_CHECKED_Q = _CHECKED_Q & Q(testrail_source_run_id__isnull=False)
_NOT_SKIPPED_Q = Q(testrail_source_skip_reason="")
# Checked, unsent cases an earlier send could not deliver (see the reason);
# they wait until unchecked.
_SKIPPED_Q = _CHECKED_Q & Q(testrail_source_result_id__isnull=True) & ~_NOT_SKIPPED_Q
# A checked case with a source run whose result has not been added yet.
_READY_Q = (
    _SOURCED_CHECKED_Q & _NOT_SKIPPED_Q & Q(testrail_source_result_id__isnull=True)
)
# A result already added whose screenshots were not all attached (needs
# `planned_shots`).
_RESUME_Q = (
    _SOURCED_CHECKED_Q
    & _NOT_SKIPPED_Q
    & Q(testrail_source_result_id__isnull=False)
    & Q(testrail_source_attachment_count__lt=F("planned_shots"))
)


# ============================================================================
# PANEL (run detail template context)
# ============================================================================


@dataclass(frozen=True)
class SourceRunLink:
    run_id: int
    url: str


@dataclass(frozen=True)
class CheckedPushPanel:
    """Run detail context for "Send checked to TestRail"; no TestRail calls.

    Counts: `total_count` cases in the run, `checked_count` checked,
    `sent_count` checked and already sent, `no_source_count` cases (checked or
    not) without a source TestRail run, `ready_count` checked cases whose
    result will be added, `resume_count` sent cases with screenshots still to
    attach, `ready_screenshot_count` screenshots the next send attaches,
    `skipped_count` checked, unsent cases an earlier send could not deliver.
    `source_runs` are the source runs of the run's checked cases.

    `status` is the run's send status, except that an interrupted (stale)
    PUSHING is shown as FAILED with `CHECKED_PUSH_INTERRUPTED_MESSAGE`.
    """

    configured: bool
    can_send: bool
    has_any_source: bool
    ready_count: int
    ready_screenshot_count: int
    no_source_count: int
    sent_count: int
    checked_count: int
    total_count: int
    source_runs: list[SourceRunLink]
    status: str
    summary: str
    error: str
    pushed_at: datetime | None
    confirm_body: str
    disabled_reason: str
    resume_count: int = 0
    skipped_count: int = 0


def _disabled_reason(
    test_run: TestRun,
    *,
    configured: bool,
    live: bool,
    pending: int,
    checked: int,
    skipped: int,
) -> str:
    if not configured:
        return "Connect TestRail under Settings first."
    if test_run.status not in _FINISHED_STATUSES:
        return "Results can be sent once the run has finished."
    if live:
        return "A send is already in progress."
    if pending:
        return ""
    if not checked:
        return "Check the cases to send first."
    if skipped:
        return (
            "No checked case is waiting to be sent. Cases marked "
            '"Not sent" in the Checked column need fixing: uncheck them, or '
            "fix them in TestRail and check them again."
        )
    return "No checked case is waiting to be sent to a source TestRail run."


def checked_push_display_status(test_run: TestRun) -> str:
    """The send status to show: an interrupted (stale) PUSHING reads as FAILED."""
    if is_checked_push_stale(test_run, timezone.now()):
        return TestRailPushStatus.FAILED
    return test_run.testrail_source_push_status


def get_checked_push_panel(test_run: TestRun) -> CheckedPushPanel:
    """Assemble the panel with one aggregate and one distinct values query."""
    project = test_run.project
    configured = project.has_testrail_settings
    pivots = _with_planned_shot_count(test_run.pivot_entries.order_by())
    totals = pivots.aggregate(
        total=Count("id"),
        checked=Count("id", filter=_CHECKED_Q),
        sent=Count(
            "id", filter=_CHECKED_Q & Q(testrail_source_result_id__isnull=False)
        ),
        sourced=Count("id", filter=Q(testrail_source_run_id__isnull=False)),
        ready=Count("id", filter=_READY_Q),
        resume=Count("id", filter=_RESUME_Q),
        skipped=Count("id", filter=_SKIPPED_Q),
        ready_shots=Coalesce(Sum("planned_shots", filter=_READY_Q), Value(0)),
        resume_shots=Coalesce(
            Sum(
                F("planned_shots") - F("testrail_source_attachment_count"),
                filter=_RESUME_Q,
            ),
            Value(0),
        ),
    )
    run_rows = (
        pivots.filter(_SOURCED_CHECKED_Q)
        .annotate(
            pending=ExpressionWrapper(_READY_Q | _RESUME_Q, output_field=BooleanField())
        )
        .values_list("testrail_source_run_id", "pending")
        .distinct()
    )
    run_ids: set[int] = set()
    pending_run_ids: set[int] = set()
    for run_id, pending in run_rows:
        if run_id is None:  # excluded by the filter; narrows the type
            continue
        run_ids.add(run_id)
        if pending:
            pending_run_ids.add(run_id)

    ready = int(totals["ready"])
    resume = int(totals["resume"])
    screenshots = int(totals["ready_shots"]) + int(totals["resume_shots"])
    pending = ready + resume
    skipped = int(totals["skipped"])
    now = timezone.now()
    stale = is_checked_push_stale(test_run, now)
    disabled_reason = _disabled_reason(
        test_run,
        configured=configured,
        live=is_checked_push_live(test_run, now),
        pending=pending,
        checked=totals["checked"],
        skipped=skipped,
    )
    return CheckedPushPanel(
        configured=configured,
        can_send=not disabled_reason,
        has_any_source=totals["sourced"] > 0,
        ready_count=ready,
        ready_screenshot_count=screenshots,
        no_source_count=totals["total"] - totals["sourced"],
        sent_count=totals["sent"],
        checked_count=totals["checked"],
        total_count=totals["total"],
        source_runs=(
            [
                SourceRunLink(
                    run_id=run_id, url=build_testrail_run_url(project, run_id)
                )
                for run_id in sorted(run_ids)
            ]
            if configured
            else []
        ),
        status=(
            TestRailPushStatus.FAILED if stale else test_run.testrail_source_push_status
        ),
        summary=test_run.testrail_source_push_summary,
        error=(
            CHECKED_PUSH_INTERRUPTED_MESSAGE
            if stale
            else test_run.testrail_source_push_error
        ),
        pushed_at=test_run.testrail_source_pushed_at,
        confirm_body=(
            format_checked_push_confirm_body(
                ready, sorted(pending_run_ids), screenshots
            )
            if pending
            else ""
        ),
        disabled_reason=disabled_reason,
        resume_count=resume,
        skipped_count=skipped,
    )


def get_source_run_url(pivot: TestRunTestCase, project: Project) -> str:
    """TestRail URL of the source run a sent case's result went to, else ""."""
    source_run_id = pivot.testrail_source_run_id
    if (
        pivot.testrail_source_result_id is None
        or source_run_id is None
        or not project.testrail_url
    ):
        return ""
    return build_testrail_run_url(project, source_run_id)


# ============================================================================
# START (validate + take the slot + dispatch)
# ============================================================================


def _has_pending(test_run: TestRun) -> bool:
    pivots = _with_planned_shot_count(test_run.pivot_entries.order_by())
    return pivots.filter(_READY_Q | _RESUME_Q).exists()


def _stale_push_q(now: datetime) -> Q:
    return Q(testrail_source_push_status=TestRailPushStatus.PUSHING) & (
        Q(testrail_source_push_started_at__isnull=True)
        | Q(testrail_source_push_started_at__lt=now - CHECKED_PUSH_STALE_AFTER)
    )


def fail_stale_checked_push(test_run: TestRun) -> None:
    """Turn an interrupted (stale) PUSHING into FAILED (one conditional UPDATE)."""
    now = timezone.now()
    if not is_checked_push_stale(test_run, now):
        return
    failed = (
        TestRun.objects.filter(pk=test_run.pk)
        .filter(_stale_push_q(now))
        .update(
            testrail_source_push_status=TestRailPushStatus.FAILED,
            testrail_source_push_error=CHECKED_PUSH_INTERRUPTED_MESSAGE,
            updated_at=now,
        )
    )
    if failed:
        test_run.testrail_source_push_status = TestRailPushStatus.FAILED
        test_run.testrail_source_push_error = CHECKED_PUSH_INTERRUPTED_MESSAGE
        _broadcast_checked_push(test_run)


def take_checked_push_slot(
    test_run: TestRun,
    *,
    run_statuses: Sequence[str],
    run_status_message: str = _RUN_NOT_FINISHED_MESSAGE,
) -> str:
    """Mark the run PUSHING unless a live send holds it (one conditional UPDATE).

    Returns the slot token (the ISO `testrail_source_push_started_at`) that
    the task must present. A PUSHING status older than the task's hard limit
    plus a minute belongs to a killed worker and may be taken over. The run
    must still be in one of `run_statuses`: a concurrent reset (WAITING) wins,
    and `RunNotFinishedError(run_status_message)` is raised. Inside a
    transaction the UPDATE locks the run row until commit, so callers that
    also lock pivots keep the order run -> pivots.
    """
    now = timezone.now()
    free_or_stale = ~Q(
        testrail_source_push_status=TestRailPushStatus.PUSHING
    ) | _stale_push_q(now)
    taken = (
        TestRun.objects.filter(pk=test_run.pk, status__in=run_statuses)
        .filter(free_or_stale)
        .update(
            testrail_source_push_status=TestRailPushStatus.PUSHING,
            testrail_source_push_started_at=now,
            testrail_source_push_error="",
            updated_at=now,
        )
    )
    if not taken:
        if not TestRun.objects.filter(pk=test_run.pk, status__in=run_statuses).exists():
            raise RunNotFinishedError(run_status_message)
        raise CheckedPushInProgressError(
            "Checked results are already being sent to TestRail."
        )
    test_run.testrail_source_push_status = TestRailPushStatus.PUSHING
    test_run.testrail_source_push_started_at = now
    test_run.testrail_source_push_error = ""
    return now.isoformat()


def dispatch_checked_push(
    test_run: TestRun,
    *,
    slot_token: str,
    pivot_ids: Sequence[int] | None,
) -> None:
    """Broadcast PUSHING and queue the send task for a slot already taken.

    Call it after the slot's transaction committed, so the task can see the
    slot. `pivot_ids` limits the send to those cases; None sends every
    checked case (the task is then queued with its original three arguments).
    On a broker error the slot is released (FAILED) and `CheckedPushError`
    raised.
    """
    _broadcast_checked_push(test_run)
    # The second argument is the task's retired links base, kept so the
    # positions match messages queued by older code.
    args: list[object] = [test_run.id, "", slot_token]
    if pivot_ids is not None:
        args.append(list(pivot_ids))
    try:
        celery_app.send_task(CHECKED_PUSH_TASK, args=args)
    except Exception as exc:
        # Broker unreachable: release the slot so the next attempt can start.
        mark_checked_push_failed(test_run, _DISPATCH_FAILED_MESSAGE)
        raise CheckedPushError(_DISPATCH_FAILED_MESSAGE) from exc


def start_checked_push(*, test_run: TestRun) -> None:
    """Validate, take the PUSHING slot, broadcast and dispatch the Celery task.

    Sends every checked case of a finished run that still has something to
    send. Raises a `CheckedPushError` subclass or `TestRailNotConfiguredError`.
    """
    if test_run.status not in _FINISHED_STATUSES:
        raise RunNotFinishedError(_RUN_NOT_FINISHED_MESSAGE)
    if not test_run.project.has_testrail_settings:
        raise TestRailNotConfiguredError(TESTRAIL_NOT_CONFIGURED_MESSAGE)
    fail_stale_checked_push(test_run)
    if not _has_pending(test_run):
        raise NothingToSendError(
            "No checked case is waiting to be sent to a source TestRail run."
        )
    slot_token = take_checked_push_slot(test_run, run_statuses=_FINISHED_STATUSES)
    dispatch_checked_push(test_run, slot_token=slot_token, pivot_ids=None)


def get_checked_push_run(test_run_id: int, slot_token: str) -> TestRun | None:
    """The run whose PUSHING slot `slot_token` still holds, else None.

    A task redelivered after its slot was taken over (or after a reset) gets
    None and must not touch anything.
    """
    try:
        started_at = datetime.fromisoformat(slot_token)
    except ValueError:
        return None
    return (
        TestRun.objects.select_related("project")
        .filter(
            pk=test_run_id,
            testrail_source_push_status=TestRailPushStatus.PUSHING,
            testrail_source_push_started_at=started_at,
        )
        .first()
    )


# ============================================================================
# PLANNING (called by the Celery task)
# ============================================================================


@dataclass(frozen=True)
class PlannedCheckedResult:
    """One checked pivot whose result and/or screenshots still go to TestRail."""

    pivot: TestRunTestCase
    case_id: int
    source_run_id: int
    status_id: int
    comment: str
    elapsed: str
    screenshots: list[TestRunScreenshot]


@dataclass(frozen=True)
class CheckedPushSkips:
    """Checked pivots the plan leaves out, by reason."""

    already_sent: int
    no_source: int
    no_case_pivots: list[TestRunTestCase]


@dataclass
class CheckedPushOutcome:
    """What one send did; stamped onto the run as its summary."""

    sent: int = 0
    screenshots: int = 0
    already_sent: int = 0
    sent_run_ids: list[int] = field(default_factory=list)
    not_in_run: dict[int, int] = field(default_factory=dict)
    missing_runs: list[int] = field(default_factory=list)
    closed_runs: list[int] = field(default_factory=list)
    no_source: int = 0
    no_case_id: int = 0
    missing_files: int = 0

    @property
    def delivered_nothing(self) -> bool:
        return self.sent == 0 and self.screenshots == 0

    @property
    def summary(self) -> str:
        return format_checked_push_summary(
            sent=self.sent,
            screenshots=self.screenshots,
            sent_run_ids=self.sent_run_ids,
            already_sent=self.already_sent,
            not_in_run=self.not_in_run,
            missing_runs=self.missing_runs,
            closed_runs=self.closed_runs,
            no_source=self.no_source,
            no_case_id=self.no_case_id,
            missing_files=self.missing_files,
        )


def _checked_by_label(user: CustomUser | None) -> str:
    if user is None:
        return _UNKNOWN_REVIEWER
    return user.get_full_name().strip() or user.email


def _existing_files(
    screenshots: Iterable[TestRunScreenshot],
) -> list[TestRunScreenshot]:
    return [
        screenshot
        for screenshot in screenshots
        if screenshot.image.name
        and screenshot.image.storage.exists(screenshot.image.name)
    ]


def _plan_pivot(
    pivot: TestRunTestCase,
    *,
    case_id: int,
    source_run_id: int,
    test_run: TestRun,
) -> PlannedCheckedResult | None:
    """The planned send for one checked pivot with a source run and a case id.

    The comment is only sent with a new result, so only then are the stored
    files checked: it counts the screenshots that will really be attached.
    The full ordered list stays the attachment counter's index.
    """
    status_id = map_pivot_status_to_testrail(pivot.status)
    if status_id is None or pivot.checked_at is None:
        return None  # a checked case always has a verdict
    screenshots: list[TestRunScreenshot] = getattr(pivot, "testrail_screenshots", [])
    if pivot.testrail_source_result_id is None:
        attached = _existing_files(screenshots)
    else:
        attached = screenshots  # the comment is not sent again
    comment = build_checked_result_comment(
        outcome_label=testrail_outcome_label(status_id),
        run_name=test_run.display_name,
        result_text=pivot_result_text(pivot),
        step_breakdown=pivot_step_breakdown(pivot),
        checked_by_label=_checked_by_label(pivot.checked_by),
        checked_at=pivot.checked_at,
        screenshot_count=len(attached),
        screenshot_steps=[
            shot.step_num for shot in attached if shot.step_num is not None
        ],
    )
    return PlannedCheckedResult(
        pivot=pivot,
        case_id=case_id,
        source_run_id=source_run_id,
        status_id=status_id,
        comment=comment,
        elapsed=format_testrail_elapsed(pivot.active_duration),
        screenshots=screenshots,
    )


def _is_complete(pivot: TestRunTestCase, screenshot_total: int) -> bool:
    return (
        pivot.testrail_source_result_id is not None
        and pivot.testrail_source_attachment_count >= screenshot_total
    )


# Everything `plan_checked_push` and the send read from a pivot and its joins.
_PLAN_PIVOT_FIELDS = (
    "id",
    "test_run",
    "status",
    "result",
    "blocked_reason",
    "execution_checkpoint",
    "segment_started_at",
    "active_duration",
    "checked_at",
    "testrail_source_run_id",
    "testrail_source_result_id",
    "testrail_source_attachment_count",
    "testrail_source_skip_reason",
    "test_case__id",
    "test_case__testrail_id",
    "checked_by__id",
    "checked_by__first_name",
    "checked_by__last_name",
    "checked_by__email",
)


def plan_checked_push(
    test_run: TestRun,
    *,
    pivot_ids: Sequence[int] | None = None,
) -> tuple[list[PlannedCheckedResult], CheckedPushSkips]:
    """Plan every checked pivot that still has something to send (2 queries).

    `pivot_ids` limits the plan to those pivots (a case send); None plans the
    whole run. Pivots an earlier send skipped (`testrail_source_skip_reason`)
    and pivots unchecked since dispatch are left out. Only the fields the
    plan reads are loaded (no logs, no test case body).
    """
    scope = Q(checked_at__isnull=False, testrail_source_skip_reason="")
    if pivot_ids is not None:
        scope &= Q(id__in=list(pivot_ids))
    pivots = (
        test_run.pivot_entries.filter(scope)
        .select_related("test_case", "checked_by")
        .only(*_PLAN_PIVOT_FIELDS)
        .prefetch_related(
            Prefetch(
                "screenshots",
                queryset=_evidence_screenshots().only(
                    "id",
                    "test_run_test_case_id",
                    "image",
                    "tool_name",
                    "step_num",
                    "created_at",
                ),
                to_attr="testrail_screenshots",
            )
        )
        .order_by(*RUN_CASE_ORDERING)
    )
    planned: list[PlannedCheckedResult] = []
    no_case_pivots: list[TestRunTestCase] = []
    already_sent = no_source = 0
    for pivot in pivots:
        source_run_id = pivot.testrail_source_run_id
        if source_run_id is None:
            no_source += 1
            continue
        if _is_complete(pivot, len(getattr(pivot, "testrail_screenshots", []))):
            already_sent += 1
            continue
        case_id = parse_testrail_case_id(pivot.test_case.testrail_id)
        if case_id is None:
            no_case_pivots.append(pivot)
            continue
        item = _plan_pivot(
            pivot,
            case_id=case_id,
            source_run_id=source_run_id,
            test_run=test_run,
        )
        if item is not None:
            planned.append(item)
    return planned, CheckedPushSkips(
        already_sent=already_sent, no_source=no_source, no_case_pivots=no_case_pivots
    )


# ============================================================================
# SENDING (called by the Celery task)
# ============================================================================


def _batched(
    items: Sequence[PlannedCheckedResult], size: int
) -> Iterator[Sequence[PlannedCheckedResult]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _fetch_source_run(client: TestRailClient, run_id: int) -> TestRailRun | None:
    try:
        return client.get_run(run_id)
    except TestRailError as exc:
        if exc.status_code in (400, 404):
            return None
        raise


def _record_results(
    batch: Sequence[PlannedCheckedResult], results: Sequence[TestRailResult]
) -> None:
    now = timezone.now()
    pivots: list[TestRunTestCase] = []
    for item, result in zip(batch, results):
        item.pivot.testrail_source_result_id = result.id
        # Kept when a response lacks it: a re-send goes to the same run's test.
        if result.test_id is not None:
            item.pivot.testrail_source_test_id = result.test_id
        item.pivot.testrail_source_sent_at = now
        pivots.append(item.pivot)
    with transaction.atomic():
        TestRunTestCase.objects.bulk_update(
            pivots,
            [
                "testrail_source_result_id",
                "testrail_source_test_id",
                "testrail_source_sent_at",
            ],
        )


def _add_results(
    client: TestRailClient, run_id: int, items: Sequence[PlannedCheckedResult]
) -> int:
    """Add results in batches, recording each batch's result ids immediately."""
    for batch in _batched(items, CHECKED_PUSH_BATCH_SIZE):
        inputs = [
            TestRailResultInput(
                case_id=item.case_id,
                status_id=item.status_id,
                comment=item.comment,
                elapsed=item.elapsed,
            )
            for item in batch
        ]
        _record_results(batch, client.add_results_for_cases(run_id, inputs))
    return len(items)


def _read_screenshot(screenshot: TestRunScreenshot) -> bytes | None:
    """The image bytes, or None when the file is gone from storage."""
    if not screenshot.image.name:
        return None
    try:
        with screenshot.image.open("rb") as handle:
            content: bytes = handle.read()
    except FileNotFoundError:
        return None
    return content


def _attach_screenshots(
    client: TestRailClient, item: PlannedCheckedResult, outcome: CheckedPushOutcome
) -> int:
    """Upload the screenshots not attached yet, one file in memory at a time.

    Returns how many were uploaded. The counter is stored after every file
    (missing ones included), so a re-send resumes at the next screenshot.
    """
    pivot = item.pivot
    result_id = pivot.testrail_source_result_id
    if result_id is None:
        return 0
    uploaded = 0
    for index in range(pivot.testrail_source_attachment_count, len(item.screenshots)):
        screenshot = item.screenshots[index]
        content = _read_screenshot(screenshot)
        if content is None:
            outcome.missing_files += 1
        else:
            attachment_id = client.add_attachment_to_result(
                result_id,
                filename=screenshot_attachment_filename(
                    item.case_id,
                    index + 1,
                    screenshot.image.name,
                    screenshot.step_num,
                ),
                content=content,
            )
            uploaded += 1
            logger.debug(
                "Attached screenshot %s to TestRail result %s as %s",
                screenshot.pk,
                result_id,
                attachment_id,
            )
        pivot.testrail_source_attachment_count = index + 1
        TestRunTestCase.objects.filter(pk=pivot.pk).update(
            testrail_source_attachment_count=index + 1
        )
    return uploaded


def _record_skips(
    pivots: Sequence[TestRunTestCase], reason: CheckedPushSkipReason
) -> None:
    """Store why these pivots could not be sent (one UPDATE)."""
    if not pivots:
        return
    TestRunTestCase.objects.filter(pk__in=[pivot.pk for pivot in pivots]).update(
        testrail_source_skip_reason=reason, updated_at=timezone.now()
    )
    for pivot in pivots:
        pivot.testrail_source_skip_reason = reason


def _send_group(
    client: TestRailClient,
    run_id: int,
    items: list[PlannedCheckedResult],
    outcome: CheckedPushOutcome,
) -> None:
    """Send one source run's results and screenshots, or record why not.

    Pivots that can never be delivered get a skip reason, so later sends
    leave them out instead of reporting them again.
    """
    run = _fetch_source_run(client, run_id)
    if run is None:
        outcome.missing_runs.append(run_id)
        _record_skips([item.pivot for item in items], CheckedPushSkipReason.RUN_MISSING)
        return
    if run.is_completed:
        outcome.closed_runs.append(run_id)
        _record_skips([item.pivot for item in items], CheckedPushSkipReason.RUN_CLOSED)
        return
    needs_result = [
        item for item in items if item.pivot.testrail_source_result_id is None
    ]
    to_add: list[PlannedCheckedResult] = []
    if needs_result:  # a resume-only group needs no case list
        case_ids = client.get_run_case_ids(run_id)
        to_add = [item for item in needs_result if item.case_id in case_ids]
        not_in_run = [
            item.pivot for item in needs_result if item.case_id not in case_ids
        ]
        if not_in_run:
            outcome.not_in_run[run_id] = len(not_in_run)
            _record_skips(not_in_run, CheckedPushSkipReason.NOT_IN_RUN)
    sent = _add_results(client, run_id, to_add)
    screenshots = sum(
        _attach_screenshots(client, item, outcome)
        for item in items
        if item.pivot.testrail_source_result_id is not None
    )
    outcome.sent += sent
    outcome.screenshots += screenshots
    if sent or screenshots:
        outcome.sent_run_ids.append(run_id)


def push_checked_results(
    test_run: TestRun,
    *,
    pivot_ids: Sequence[int] | None = None,
) -> CheckedPushOutcome:
    """Send every checked, unsent result to its source run. Called by the task.

    `pivot_ids` limits the send to those pivots (a case send); None sends
    every checked case of the run.

    Raises TestRailError / TestRailNotConfiguredError; the task stamps the run
    as failed. Everything recorded before the error survives, so a re-send
    resumes without duplicate results or attachments.
    """
    planned, skips = plan_checked_push(test_run, pivot_ids=pivot_ids)
    outcome = CheckedPushOutcome(
        already_sent=skips.already_sent,
        no_source=skips.no_source,
        no_case_id=len(skips.no_case_pivots),
    )
    _record_skips(skips.no_case_pivots, CheckedPushSkipReason.NO_CASE_ID)
    if not planned:
        return outcome
    by_run = sorted(planned, key=lambda item: item.source_run_id)
    with build_testrail_client(test_run.project) as client:
        for run_id, group in groupby(by_run, key=lambda item: item.source_run_id):
            _send_group(client, run_id, list(group), outcome)
    return outcome


# ============================================================================
# MARK RESULT (called by the Celery task)
# ============================================================================


def mark_checked_push_succeeded(test_run: TestRun, outcome: CheckedPushOutcome) -> None:
    """Stamp the outcome; FAILED when nothing went out because runs were unusable."""
    summary = outcome.summary
    failed = outcome.delivered_nothing and bool(
        outcome.closed_runs or outcome.missing_runs
    )
    test_run.testrail_source_push_summary = summary
    if failed:
        test_run.testrail_source_push_status = TestRailPushStatus.FAILED
        test_run.testrail_source_push_error = summary
    else:
        test_run.testrail_source_push_status = TestRailPushStatus.PUSHED
        test_run.testrail_source_push_error = ""
        test_run.testrail_source_pushed_at = timezone.now()
    test_run.save(
        update_fields=[
            "testrail_source_push_status",
            "testrail_source_pushed_at",
            "testrail_source_push_error",
            "testrail_source_push_summary",
            "updated_at",
        ]
    )
    _broadcast_checked_push(test_run)


def mark_checked_push_failed(test_run: TestRun, message: str) -> None:
    """Stamp the run as failed with a user-safe message, and broadcast it."""
    test_run.testrail_source_push_status = TestRailPushStatus.FAILED
    test_run.testrail_source_push_error = message
    test_run.save(
        update_fields=[
            "testrail_source_push_status",
            "testrail_source_push_error",
            "updated_at",
        ]
    )
    _broadcast_checked_push(test_run)


# ============================================================================
# BROADCAST
# ============================================================================


class CheckedPushEvent(TypedDict):
    type: str
    push_status: str


def _broadcast_checked_push(test_run: TestRun) -> None:
    layer: Any = get_channel_layer()  # channels ships no types for the layer
    if layer is None:
        return
    event: CheckedPushEvent = {
        "type": "test_run.testrail_source_push",
        "push_status": test_run.testrail_source_push_status,
    }
    async_to_sync(layer.group_send)(_test_run_group(test_run.id), event)
