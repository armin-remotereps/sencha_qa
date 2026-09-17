"""Push a finished test run's results back to TestRail.

Plans which pivots are pushable, ensures a TestRail run exists for a Punk
Hazard run, sends changed results in batches, and tracks push status so the
run detail page and its WebSocket group can show progress.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from auto_tester.celery import app as celery_app
from projects.models import (
    Project,
    TestRailPushStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
)
from projects.services import _test_run_group
from projects.testrail_client import (
    TestRailClient,
    TestRailError,
    TestRailResult,
    TestRailResultInput,
    TestRailRun,
)
from projects.testrail_mapping import (
    build_result_comment,
    comment_hash,
    format_testrail_elapsed,
    map_pivot_status_to_testrail,
    parse_testrail_case_id,
    testrail_outcome_label,
)
from projects.testrail_services import (
    TestRailImportTarget,
    TestRailNotConfiguredError,
    TestRailPickerState,
    build_testrail_client,
    get_testrail_picker_state,
)

logger = logging.getLogger(__name__)

PUSH_BATCH_SIZE = 100


# ============================================================================
# PLANNING
# ============================================================================


@dataclass(frozen=True)
class PlannedResult:
    """One pivot ready to be sent to TestRail."""

    pivot: TestRunTestCase
    case_id: int
    status_id: int
    comment: str
    elapsed: str


@dataclass(frozen=True)
class TestRailPushPlan:
    """The outcome of planning a push: what to send, and what was skipped."""

    results: list[PlannedResult]
    changed: list[PlannedResult]
    skipped_no_case_id: int
    skipped_not_pushable: int


@dataclass(frozen=True)
class _PivotPlanOutcome:
    """Per-pivot planning result, before it is folded into a TestRailPushPlan."""

    planned: PlannedResult | None
    no_case_id: bool
    not_pushable: bool


def build_case_detail_url(
    test_run: TestRun, pivot_id: int, *, links_base_url: str
) -> str:
    """Absolute URL to a run's case detail page, used in TestRail comments."""
    path = reverse(
        "projects:test_run_case_detail",
        args=[test_run.project_id, test_run.id, pivot_id],
    )
    return f"{links_base_url.rstrip('/')}{path}"


def _plan_pivot(
    pivot: TestRunTestCase, *, test_run: TestRun, links_base_url: str
) -> _PivotPlanOutcome:
    """Map one pivot to a planned result, or record why it cannot be pushed."""
    status_id = map_pivot_status_to_testrail(pivot.status)
    if status_id is None:
        return _PivotPlanOutcome(planned=None, no_case_id=False, not_pushable=True)
    case_id = parse_testrail_case_id(pivot.test_case.testrail_id)
    if case_id is None:
        return _PivotPlanOutcome(planned=None, no_case_id=True, not_pushable=False)
    comment = build_result_comment(
        outcome_label=testrail_outcome_label(status_id),
        run_name=test_run.display_name,
        result_text=pivot.result,
        case_url=build_case_detail_url(
            test_run, pivot.id, links_base_url=links_base_url
        ),
    )
    planned = PlannedResult(
        pivot=pivot,
        case_id=case_id,
        status_id=status_id,
        comment=comment,
        elapsed=format_testrail_elapsed(pivot.started_at, pivot.finished_at),
    )
    return _PivotPlanOutcome(planned=planned, no_case_id=False, not_pushable=False)


def _is_changed(result: PlannedResult) -> bool:
    pivot = result.pivot
    return (
        pivot.testrail_pushed_status_id != result.status_id
        or pivot.testrail_pushed_comment_hash != comment_hash(result.comment)
    )


def plan_testrail_push(test_run: TestRun, *, links_base_url: str) -> TestRailPushPlan:
    """Build the push plan for a run: one query over its pivots and cases."""
    pivots = test_run.pivot_entries.select_related("test_case").order_by("id")
    outcomes = [
        _plan_pivot(pivot, test_run=test_run, links_base_url=links_base_url)
        for pivot in pivots
    ]
    results = [outcome.planned for outcome in outcomes if outcome.planned is not None]
    return TestRailPushPlan(
        results=results,
        changed=[result for result in results if _is_changed(result)],
        skipped_no_case_id=sum(outcome.no_case_id for outcome in outcomes),
        skipped_not_pushable=sum(outcome.not_pushable for outcome in outcomes),
    )


def suggest_testrail_target(test_run: TestRun) -> tuple[int | None, int | None]:
    """Most common (testrail_project_id, testrail_suite_id) among the run's
    cases' uploads. (None, None) when no case came from a TestRail import."""
    pairs = test_run.pivot_entries.filter(
        test_case__upload__testrail_project_id__isnull=False,
        test_case__upload__testrail_suite_id__isnull=False,
    ).values_list(
        "test_case__upload__testrail_project_id",
        "test_case__upload__testrail_suite_id",
    )
    counts = Counter(pairs)
    if not counts:
        return (None, None)
    most_common_pair, _count = counts.most_common(1)[0]
    return most_common_pair


# ============================================================================
# PANEL (run detail template context)
# ============================================================================


@dataclass(frozen=True)
class TestRailPushPanel:
    """Everything the run detail template needs to render the push UI."""

    configured: bool
    can_push: bool
    has_target: bool
    picker: TestRailPickerState | None
    suggested_project_id: int | None
    pending_count: int
    run_url: str


def build_testrail_run_url(project: Project, run_id: int) -> str:
    """Public TestRail URL for a run, used on the run detail page."""
    return f"{project.testrail_url}/index.php?/runs/view/{run_id}"


def can_push_testrail_results(test_run: TestRun) -> bool:
    """True when the run is finished and not already mid-push."""
    return (
        test_run.status in (TestRunStatus.DONE, TestRunStatus.CANCELLED)
        and test_run.testrail_push_status != TestRailPushStatus.PUSHING
    )


def _picker_state_for_panel(
    test_run: TestRun, *, can_push: bool, has_target: bool
) -> tuple[TestRailPickerState | None, int | None]:
    if not (can_push and not has_target):
        return None, None
    picker = get_testrail_picker_state(test_run.project)
    suggested_project_id, _suggested_suite_id = suggest_testrail_target(test_run)
    return picker, suggested_project_id


def _pending_count(test_run: TestRun, *, can_push: bool, links_base_url: str) -> int:
    if not can_push:
        return 0
    plan = plan_testrail_push(test_run, links_base_url=links_base_url)
    return len(plan.changed)


def get_testrail_push_panel(
    test_run: TestRun, *, links_base_url: str
) -> TestRailPushPanel:
    """Assemble the run detail template's push panel context in one call."""
    project = test_run.project
    configured = project.has_testrail_settings
    can_push = configured and can_push_testrail_results(test_run)
    has_target = test_run.has_testrail_target
    picker, suggested_project_id = _picker_state_for_panel(
        test_run, can_push=can_push, has_target=has_target
    )
    run_url = (
        build_testrail_run_url(project, test_run.testrail_run_id)
        if test_run.testrail_run_id
        else ""
    )
    return TestRailPushPanel(
        configured=configured,
        can_push=can_push,
        has_target=has_target,
        picker=picker,
        suggested_project_id=suggested_project_id,
        pending_count=_pending_count(
            test_run, can_push=can_push, links_base_url=links_base_url
        ),
        run_url=run_url,
    )


def set_testrail_target(test_run: TestRun, target: TestRailImportTarget) -> None:
    """Store the chosen TestRail project + suite on the run before its first push."""
    test_run.testrail_project_id = target.testrail_project.id
    test_run.testrail_suite_id = target.suite.id
    test_run.save(
        update_fields=["testrail_project_id", "testrail_suite_id", "updated_at"]
    )


# ============================================================================
# START (validate + dispatch)
# ============================================================================


def _validate_can_start_push(test_run: TestRun) -> None:
    if test_run.status not in (TestRunStatus.DONE, TestRunStatus.CANCELLED):
        raise ValueError("Only finished runs can be pushed to TestRail.")
    if test_run.testrail_push_status == TestRailPushStatus.PUSHING:
        raise ValueError("This run is already being pushed to TestRail.")
    if not test_run.project.has_testrail_settings:
        raise TestRailNotConfiguredError(
            "TestRail is not configured for this project. Add the URL, email and API key under Settings."
        )
    if not test_run.has_testrail_target:
        raise ValueError("Choose a TestRail project and suite first.")


def start_testrail_results_push(*, test_run: TestRun, links_base_url: str) -> None:
    """Validate the run, mark it pushing, and dispatch the Celery push task."""
    _validate_can_start_push(test_run)
    test_run.testrail_push_status = TestRailPushStatus.PUSHING
    test_run.testrail_push_error = ""
    test_run.save(
        update_fields=["testrail_push_status", "testrail_push_error", "updated_at"]
    )
    _broadcast_testrail_push(test_run)
    celery_app.send_task(
        "projects.tasks.push_test_run_results",
        args=[test_run.id, links_base_url],
    )


# ============================================================================
# PUSH EXECUTION (called by the Celery task)
# ============================================================================


@dataclass(frozen=True)
class TestRailPushOutcome:
    """What happened during one push, used to stamp the run's summary."""

    pushed: int
    unchanged: int
    skipped_no_case_id: int
    testrail_run_id: int


def _previously_pushed_case_ids(test_run: TestRun) -> set[int]:
    raw_ids = test_run.pivot_entries.filter(
        testrail_result_id__isnull=False
    ).values_list("test_case__testrail_id", flat=True)
    parsed = (parse_testrail_case_id(raw_id) for raw_id in raw_ids)
    return {case_id for case_id in parsed if case_id is not None}


def _collect_case_ids(test_run: TestRun, plan: TestRailPushPlan) -> list[int]:
    case_ids = {result.case_id for result in plan.results}
    case_ids.update(_previously_pushed_case_ids(test_run))
    return sorted(case_ids)


def _testrail_run_description(test_run: TestRun, *, links_base_url: str) -> str:
    run_detail_path = reverse(
        "projects:test_run_detail", args=[test_run.project_id, test_run.id]
    )
    run_detail_url = f"{links_base_url.rstrip('/')}{run_detail_path}"
    return (
        f"Results pushed from Punk Hazard project “{test_run.project.name}”.\n"
        f"{run_detail_url}"
    )


def _create_testrail_run(
    client: TestRailClient,
    test_run: TestRun,
    case_ids: Sequence[int],
    *,
    links_base_url: str,
) -> int:
    project_id = test_run.testrail_project_id
    suite_id = test_run.testrail_suite_id
    if project_id is None or suite_id is None:
        raise ValueError("Choose a TestRail project and suite first.")
    run = client.add_run(
        project_id,
        suite_id=suite_id,
        name=f"Punk Hazard: {test_run.display_name}",
        description=_testrail_run_description(test_run, links_base_url=links_base_url),
        case_ids=case_ids,
    )
    test_run.testrail_run_id = run.id
    test_run.save(update_fields=["testrail_run_id", "updated_at"])
    return run.id


def _fetch_existing_run(client: TestRailClient, run_id: int) -> TestRailRun | None:
    try:
        return client.get_run(run_id)
    except TestRailError as exc:
        if exc.status_code in (400, 404):
            return None
        raise


def _reset_pivot_tracking(test_run: TestRun) -> None:
    """Clear per-pivot push tracking so a fresh TestRail run gets every result again."""
    test_run.pivot_entries.update(
        testrail_result_id=None,
        testrail_pushed_status_id=None,
        testrail_pushed_comment_hash="",
        testrail_pushed_at=None,
    )


def _ensure_testrail_run(
    client: TestRailClient,
    test_run: TestRun,
    plan: TestRailPushPlan,
    *,
    links_base_url: str,
) -> tuple[int, TestRailPushPlan]:
    """Create, reuse or recreate the TestRail run this push writes into.

    Returns the run id and the plan to push against it — recomputed (with
    everything marked changed) when a closed/deleted run is replaced.
    """
    case_ids = _collect_case_ids(test_run, plan)
    if test_run.testrail_run_id is None:
        run_id = _create_testrail_run(
            client, test_run, case_ids, links_base_url=links_base_url
        )
        return run_id, plan

    existing = _fetch_existing_run(client, test_run.testrail_run_id)
    if existing is None or existing.is_completed:
        run_id = _create_testrail_run(
            client, test_run, case_ids, links_base_url=links_base_url
        )
        _reset_pivot_tracking(test_run)
        return run_id, plan_testrail_push(test_run, links_base_url=links_base_url)

    client.update_run(existing.id, case_ids=case_ids)
    return existing.id, plan


def _batched(
    items: Sequence[PlannedResult], size: int
) -> Iterator[Sequence[PlannedResult]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _record_pushed_results(
    batch: Sequence[PlannedResult], results: Sequence[TestRailResult]
) -> None:
    now = timezone.now()
    pivots: list[TestRunTestCase] = []
    for planned, result in zip(batch, results):
        pivot = planned.pivot
        pivot.testrail_result_id = result.id
        pivot.testrail_pushed_status_id = planned.status_id
        pivot.testrail_pushed_comment_hash = comment_hash(planned.comment)
        pivot.testrail_pushed_at = now
        pivots.append(pivot)
    with transaction.atomic():
        TestRunTestCase.objects.bulk_update(
            pivots,
            [
                "testrail_result_id",
                "testrail_pushed_status_id",
                "testrail_pushed_comment_hash",
                "testrail_pushed_at",
            ],
        )


def _push_batch(
    client: TestRailClient, run_id: int, batch: Sequence[PlannedResult]
) -> None:
    inputs = [
        TestRailResultInput(
            case_id=result.case_id,
            status_id=result.status_id,
            comment=result.comment,
            elapsed=result.elapsed,
        )
        for result in batch
    ]
    results = client.add_results_for_cases(run_id, inputs)
    _record_pushed_results(batch, results)


def push_test_run_results(
    test_run: TestRun, *, links_base_url: str
) -> TestRailPushOutcome:
    """Push a run's changed results to TestRail. Called by the Celery task.

    Raises TestRailError / TestRailNotConfiguredError; callers stamp the run
    as failed rather than this function catching them.
    """
    plan = plan_testrail_push(test_run, links_base_url=links_base_url)
    if not plan.results:
        return TestRailPushOutcome(
            pushed=0,
            unchanged=0,
            skipped_no_case_id=plan.skipped_no_case_id,
            testrail_run_id=test_run.testrail_run_id or 0,
        )
    with build_testrail_client(test_run.project) as client:
        run_id, plan = _ensure_testrail_run(
            client, test_run, plan, links_base_url=links_base_url
        )
        for batch in _batched(plan.changed, PUSH_BATCH_SIZE):
            _push_batch(client, run_id, batch)
    return TestRailPushOutcome(
        pushed=len(plan.changed),
        unchanged=len(plan.results) - len(plan.changed),
        skipped_no_case_id=plan.skipped_no_case_id,
        testrail_run_id=run_id,
    )


# ============================================================================
# MARK RESULT (called by the Celery task after push_test_run_results)
# ============================================================================


def _pluralize(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _build_push_summary(outcome: TestRailPushOutcome) -> str:
    """e.g. "12 results pushed · 3 unchanged · 1 without a TestRail ID"."""
    parts = [f"{_pluralize(outcome.pushed, 'result', 'results')} pushed"]
    if outcome.unchanged:
        parts.append(f"{outcome.unchanged} unchanged")
    if outcome.skipped_no_case_id:
        parts.append(f"{outcome.skipped_no_case_id} without a TestRail ID")
    return " · ".join(parts)


def mark_testrail_push_succeeded(
    test_run: TestRun, outcome: TestRailPushOutcome
) -> None:
    """Stamp the run as pushed with a summary, and broadcast the new status."""
    test_run.testrail_push_status = TestRailPushStatus.PUSHED
    test_run.testrail_pushed_at = timezone.now()
    test_run.testrail_push_error = ""
    test_run.testrail_push_summary = _build_push_summary(outcome)
    test_run.save(
        update_fields=[
            "testrail_push_status",
            "testrail_pushed_at",
            "testrail_push_error",
            "testrail_push_summary",
            "updated_at",
        ]
    )
    _broadcast_testrail_push(test_run)


def mark_testrail_push_failed(test_run: TestRun, message: str) -> None:
    """Stamp the run as failed with the TestRail error message, and broadcast it."""
    test_run.testrail_push_status = TestRailPushStatus.FAILED
    test_run.testrail_push_error = message
    test_run.save(
        update_fields=["testrail_push_status", "testrail_push_error", "updated_at"]
    )
    _broadcast_testrail_push(test_run)


# ============================================================================
# BROADCAST
# ============================================================================


class TestRailPushEvent(TypedDict):
    type: str
    push_status: str


def _broadcast_testrail_push(test_run: TestRun) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: TestRailPushEvent = {
        "type": "test_run.testrail_push",
        "push_status": test_run.testrail_push_status,
    }
    async_to_sync(layer.group_send)(_test_run_group(test_run.id), event)
