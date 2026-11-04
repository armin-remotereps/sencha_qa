"""Case analysis badges for the run case table and the case page."""

from __future__ import annotations

from collections.abc import Collection

from run_triage.models import TriageCase
from run_triage.services.taxonomy_text import category_label, severity_label
from run_triage.services.triage_state import is_stale
from run_triage.types import TriageBadge


def triage_badges_for(pivot_ids: Collection[int]) -> dict[int, TriageBadge]:
    """The analysis badge of each of these cases that has a row (one query)."""
    if not pivot_ids:
        return {}
    badges: dict[int, TriageBadge] = {}
    for (
        pivot_id,
        category,
        severity,
        classified_status,
        classified_finished_at,
        status,
        finished_at,
    ) in TriageCase.objects.filter(
        test_run_test_case_id__in=list(pivot_ids)
    ).values_list(
        "test_run_test_case_id",
        "category",
        "severity",
        "classified_status",
        "classified_finished_at",
        "test_run_test_case__status",
        "test_run_test_case__finished_at",
    ):
        badges[pivot_id] = TriageBadge(
            category=category,
            category_label=category_label(category),
            severity=severity,
            severity_label=severity_label(severity),
            stale=is_stale(
                classified_status, classified_finished_at, status, finished_at
            ),
        )
    return badges
