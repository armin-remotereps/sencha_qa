"""The Failures page list: one page of Failed run cases, and the run choices
of the run filter."""

from __future__ import annotations

from collections.abc import Sequence

from django.core.paginator import Page, Paginator
from django.db.models import Exists, OuterRef

from dashboard.services.failed_case_filters import (
    FailedCaseFilters,
    failed_case_list_queryset,
)
from projects.models import TestRun, TestRunTestCase, TestRunTestCaseStatus

# The run filter offers at most this many runs (newest first).
RUN_CHOICE_LIMIT = 200


def list_failed_cases(
    project_ids: Sequence[int],
    filters: FailedCaseFilters,
    *,
    page: int,
    per_page: int,
) -> Page[TestRunTestCase]:
    """One page of Failed run cases, newest failure first (count + page: 2
    queries). Rows carry ``verdict`` (the truncated result), never logs."""
    paginator = Paginator(failed_case_list_queryset(project_ids, filters), per_page)
    return paginator.get_page(page)


def failed_case_run_choices(
    project_ids: Sequence[int], project_id: int | None
) -> list[tuple[int, str]]:
    """``(id, label)`` of the project's runs that have a Failed case, newest
    first, at most ``RUN_CHOICE_LIMIT`` (1 query; none without a project or
    for a project outside ``project_ids``)."""
    if project_id is None or project_id not in project_ids:
        return []
    has_failed_case = Exists(
        TestRunTestCase.objects.filter(
            test_run_id=OuterRef("pk"), status=TestRunTestCaseStatus.FAILED
        )
    )
    rows = (
        TestRun.objects.filter(has_failed_case, project_id=project_id)
        .order_by("-created_at", "-id")
        .values_list("id", "name")[:RUN_CHOICE_LIMIT]
    )
    return [(run_id, name or f"Run #{run_id}") for run_id, name in rows]
