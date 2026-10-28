"""The dashboard's compact "Failed cases" widget: the last 7 days at a glance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.urls import reverse

from dashboard.services.failed_case_filters import (
    FailedCaseFilters,
    failed_case_queryset,
)

FAILED_WIDGET_DAYS = 7
FAILED_WIDGET_TOP_PROJECTS = 5
FAILED_WIDGET_LATEST = 5


@dataclass(frozen=True)
class ProjectFailureCount:
    project_id: int
    project_name: str
    failures: int
    # The failures page filtered on this project and the widget's window.
    url: str


@dataclass(frozen=True)
class LatestFailure:
    pivot_id: int
    case_title: str
    testrail_id: str
    project_name: str
    run_name: str
    finished_at: datetime | None
    url: str


@dataclass(frozen=True)
class FailedCaseWidget:
    since: date
    total: int
    not_checked: int
    top_projects: list[ProjectFailureCount]
    latest: list[LatestFailure]
    # The whole failures page (all time).
    all_url: str


def _failures_url(params: dict[str, str]) -> str:
    return f"{reverse('dashboard:failed_cases')}?{urlencode(params)}"


def _window_start(now: datetime) -> date:
    """The first UTC day of the window: the last ``FAILED_WIDGET_DAYS`` UTC
    days, today included (the Failures page reads ``date_from`` as a UTC day)."""
    return now.astimezone(UTC).date() - timedelta(days=FAILED_WIDGET_DAYS - 1)


def get_failed_case_widget(
    project_ids: Sequence[int], now: datetime
) -> FailedCaseWidget:
    """Totals, top projects and latest failures of the last 7 days (3 queries).

    The window is exactly the Failures page filtered on ``date_from=since``
    (``failed_case_queryset``), so every count matches the page it links to.
    """
    since = _window_start(now)
    date_from = since.isoformat()
    scope = failed_case_queryset(project_ids, FailedCaseFilters(date_from=since))

    totals = scope.aggregate(
        total=Count("id"), not_checked=Count("id", filter=Q(checked_at__isnull=True))
    )
    top_rows = (
        scope.values("test_run__project_id", "test_run__project__name")
        .annotate(failures=Count("id"))
        .order_by("-failures", "test_run__project__name")[:FAILED_WIDGET_TOP_PROJECTS]
    )
    latest_rows = (
        scope.select_related("test_case", "test_run__project")
        .only(
            "id",
            "finished_at",
            "test_case__id",
            "test_case__title",
            "test_case__testrail_id",
            "test_run__id",
            "test_run__name",
            "test_run__project__id",
            "test_run__project__name",
        )
        .order_by("-finished_at", "-id")[:FAILED_WIDGET_LATEST]
    )

    return FailedCaseWidget(
        since=since,
        total=totals["total"] or 0,
        not_checked=totals["not_checked"] or 0,
        top_projects=[
            ProjectFailureCount(
                project_id=row["test_run__project_id"],
                project_name=row["test_run__project__name"],
                failures=row["failures"],
                url=_failures_url(
                    {
                        "project": str(row["test_run__project_id"]),
                        "date_from": date_from,
                    }
                ),
            )
            for row in top_rows
        ],
        latest=[
            LatestFailure(
                pivot_id=pivot.pk,
                case_title=pivot.test_case.title,
                testrail_id=pivot.test_case.testrail_id,
                project_name=pivot.test_run.project.name,
                run_name=pivot.test_run.display_name,
                finished_at=pivot.finished_at,
                url=reverse(
                    "projects:test_run_case_detail",
                    kwargs={
                        "project_id": pivot.test_run.project_id,
                        "test_run_id": pivot.test_run_id,
                        "pivot_id": pivot.pk,
                    },
                ),
            )
            for pivot in latest_rows
        ],
        all_url=reverse("dashboard:failed_cases"),
    )
