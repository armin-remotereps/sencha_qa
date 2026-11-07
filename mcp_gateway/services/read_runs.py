"""Use case: the MCP tester lists the project's Manual runs (list_runs).

Newest first, keyset-paged on the run id (``RUNS_PAGE_SIZE`` per page), with
case counts per status. One query whatever the page size.
"""

from __future__ import annotations

from typing import Final, Literal

from django.db.models import Count, Q

from mcp_gateway.exceptions import McpToolError
from mcp_gateway.schemas import ListRunsRequest, ListRunsResponse, RunCounts, RunItem
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.untrusted_text import case_line
from projects.models import (
    ACTIVE_RUN_STATUSES,
    RunHelpMode,
    TestRun,
    TestRunStatus,
    TestRunTestCaseStatus,
)

RUNS_PAGE_SIZE: Final = 50
_BAD_CURSOR: Final = "next_cursor is not valid; list from the start."
_STATUS_FILTERS: Final[dict[str, tuple[str, ...]]] = {
    "running": ACTIVE_RUN_STATUSES,
    "finished": (TestRunStatus.DONE, TestRunStatus.CANCELLED),
}


def _parse_cursor(cursor: str | None) -> int | None:
    if cursor is None or cursor == "":
        return None
    if not (cursor.isascii() and cursor.isdigit()):
        raise McpToolError(_BAD_CURSOR)
    return int(cursor)


def run_status_label(status: str) -> str:
    """A run status as the client reads it (running, draft, done, cancelled)."""
    if status in ACTIVE_RUN_STATUSES:
        return "running"
    if status == TestRunStatus.WAITING:
        return "draft"
    return status


def read_runs(call: McpCall, request: ListRunsRequest) -> ListRunsResponse:
    status: Literal["running", "finished", "all"] = request.status
    before = _parse_cursor(request.cursor)
    runs = TestRun.objects.filter(
        project_id=call.project.pk, help_mode=RunHelpMode.MANUAL
    )
    if status in _STATUS_FILTERS:
        runs = runs.filter(status__in=_STATUS_FILTERS[status])
    if before is not None:
        runs = runs.filter(pk__lt=before)
    rows = list(
        runs.order_by("-pk")
        .annotate(
            n_total=Count("pivot_entries"),
            n_created=Count(
                "pivot_entries",
                filter=Q(pivot_entries__status=TestRunTestCaseStatus.CREATED),
            ),
            n_progress=Count(
                "pivot_entries",
                filter=Q(pivot_entries__status=TestRunTestCaseStatus.IN_PROGRESS),
            ),
            n_passed=Count(
                "pivot_entries",
                filter=Q(pivot_entries__status=TestRunTestCaseStatus.SUCCESS),
            ),
            n_failed=Count(
                "pivot_entries",
                filter=Q(pivot_entries__status=TestRunTestCaseStatus.FAILED),
            ),
            n_blocked=Count(
                "pivot_entries",
                filter=Q(pivot_entries__status=TestRunTestCaseStatus.BLOCKED),
            ),
        )
        .values_list(
            "id",
            "name",
            "status",
            "started_at",
            "finished_at",
            "n_total",
            "n_created",
            "n_progress",
            "n_passed",
            "n_failed",
            "n_blocked",
        )[: RUNS_PAGE_SIZE + 1]
    )
    items = [
        RunItem(
            id=run_id,
            name=case_line(name or f"Run #{run_id}"),
            status=run_status_label(run_status),
            started_at=started.isoformat() if started is not None else None,
            finished_at=finished.isoformat() if finished is not None else None,
            counts=RunCounts(
                total=total,
                not_started=created,
                in_progress=progress,
                passed=passed,
                failed=failed,
                blocked=blocked,
                other=total - created - progress - passed - failed - blocked,
            ),
        )
        for (
            run_id,
            name,
            run_status,
            started,
            finished,
            total,
            created,
            progress,
            passed,
            failed,
            blocked,
        ) in rows[:RUNS_PAGE_SIZE]
    ]
    more = len(rows) > RUNS_PAGE_SIZE
    return ListRunsResponse(
        runs=items, next_cursor=str(items[-1].id) if more and items else None
    )
