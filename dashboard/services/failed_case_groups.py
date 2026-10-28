"""Top failing test cases: the Failures page's "By test case" view."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TypedDict, cast

from django.core.paginator import Page, Paginator
from django.db.models import Count, Max

from dashboard.services.failed_case_filters import (
    FailedCaseFilters,
    failed_case_queryset,
)
from projects.models import TestRunTestCase


@dataclass(frozen=True)
class FailingCaseRow:
    """One test case with its Failed run cases in the current filters.

    ``latest_*`` point at its newest Failed run case (highest pivot id: pivots
    are created when cases are added to a run, so the highest is the newest).
    """

    test_case_id: int
    title: str
    testrail_id: str
    project_id: int
    project_name: str
    failures: int
    runs: int
    last_failed: datetime | None
    latest_pivot_id: int
    latest_run_id: int


class _GroupValues(TypedDict):
    test_case_id: int
    test_case__title: str
    test_case__testrail_id: str
    test_run__project_id: int
    test_run__project__name: str
    failures: int
    runs: int
    last_failed: datetime | None
    latest_pivot_id: int


def list_failing_test_cases(
    project_ids: Sequence[int],
    filters: FailedCaseFilters,
    *,
    page: int,
    per_page: int,
) -> Page[FailingCaseRow]:
    """One page of test cases by number of failures, then latest failure
    (count + page + the latest runs: 3 queries). The run filter is ignored:
    grouping a single run makes no sense."""
    grouped = (
        failed_case_queryset(project_ids, replace(filters, run_id=None))
        .order_by()
        .values(
            "test_case_id",
            "test_case__title",
            "test_case__testrail_id",
            "test_run__project_id",
            "test_run__project__name",
        )
        .annotate(
            failures=Count("id"),
            runs=Count("test_run_id", distinct=True),
            last_failed=Max("finished_at"),
            latest_pivot_id=Max("id"),
        )
        .order_by("-failures", "-last_failed", "test_case_id")
    )
    paginator = Paginator(grouped, per_page)
    group_page = paginator.get_page(page)
    # values() rows: the dict shape is fixed by the values() call above.
    groups = cast(list[_GroupValues], list(group_page.object_list))
    latest_ids = [group["latest_pivot_id"] for group in groups]
    run_of_pivot = dict(
        TestRunTestCase.objects.filter(id__in=latest_ids).values_list(
            "id", "test_run_id"
        )
    )
    rows = [
        FailingCaseRow(
            test_case_id=group["test_case_id"],
            title=group["test_case__title"],
            testrail_id=group["test_case__testrail_id"],
            project_id=group["test_run__project_id"],
            project_name=group["test_run__project__name"],
            failures=group["failures"],
            runs=group["runs"],
            last_failed=group["last_failed"],
            latest_pivot_id=group["latest_pivot_id"],
            latest_run_id=run_of_pivot[group["latest_pivot_id"]],
        )
        for group in groups
        if group["latest_pivot_id"] in run_of_pivot
    ]
    # The paginator counted the grouped dicts; this page holds the same rows
    # mapped to FailingCaseRow, which django-stubs' Page[T] cannot express.
    return Page(rows, group_page.number, paginator)  # type: ignore[arg-type]
