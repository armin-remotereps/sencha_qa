"""Retiring pending result-chat (review) redesign drafts.

A leaf module (models only): ``projects.services`` uses it on a run reset and
``case_review_redesign_services`` when a newer draft replaces the pending one.
A superseded draft is shown as Superseded with its steps hidden, so its step lists are cleared.
"""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils import timezone

from projects.models import CasePlanRevision, PlanRevisionSource, PlanRevisionStatus


def supersede_case_review_drafts(pivot_id: int) -> None:
    """Retire the case's pending review draft."""
    _supersede(CasePlanRevision.objects.filter(test_run_test_case_id=pivot_id))


def supersede_run_review_drafts(test_run_id: int) -> None:
    """Retire the pending review drafts of every case of the run."""
    _supersede(
        CasePlanRevision.objects.filter(test_run_test_case__test_run_id=test_run_id)
    )


def _supersede(revisions: QuerySet[CasePlanRevision]) -> None:
    revisions.filter(
        source=PlanRevisionSource.REVIEW, status=PlanRevisionStatus.PROPOSED
    ).update(
        status=PlanRevisionStatus.SUPERSEDED,
        decided_at=timezone.now(),
        steps=[],
        previous_steps=[],
    )
