"""The dashboard's compact "Failed cases" widget: the last 7 days at a glance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from django.db.models import Count, Q, QuerySet
from django.urls import reverse

from projects.models import TestRunTestCase, TestRunTestCaseStatus

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


def _window_failures(
    project_ids: list[int], since: datetime
) -> QuerySet[TestRunTestCase]:
    # Same scope as the failures page (Failed, member projects, finished in the
    # window); served by the (status, -finished_at) index.
    return TestRunTestCase.objects.filter(
        status=TestRunTestCaseStatus.FAILED,
        test_run__project_id__in=project_ids,
        finished_at__gte=since,
    )


def _failures_url(params: dict[str, str]) -> str:
    return f"{reverse('dashboard:failed_cases')}?{urlencode(params)}"


def get_failed_case_widget(project_ids: list[int], now: datetime) -> FailedCaseWidget:
    """Totals, top projects and latest failures of the last 7 days (3 queries)."""
    since_at = now - timedelta(days=FAILED_WIDGET_DAYS)
    since = since_at.date()
    date_from = since.isoformat()
    scope = _window_failures(project_ids, since_at)

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
