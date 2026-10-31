"""The Requirements page of the Machine readiness tab (read only).

Groups of cases by the stage 0 they need, one page of requirements, and the
analysis state. No LLM, no runner call.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.paginator import Page, Paginator

from machine_readiness.models import Stage0Requirement
from machine_readiness.services.analyse_preconditions import analysis_running
from machine_readiness.services.stage0_groups import Stage0Group, list_stage0_groups

REQUIREMENTS_PER_PAGE = 50


@dataclass(frozen=True)
class RequirementsPage:
    groups: tuple[Stage0Group, ...]
    requirements: Page[Stage0Requirement]
    # Cases whose preconditions have no analysis yet.
    unanalysed_count: int
    analysis_running: bool


def get_requirements_page(project_id: int, *, page: int) -> RequirementsPage:
    """Groups (2 queries), one page of requirements (count + page), and the
    analysis lock (one cache read)."""
    groups = tuple(list_stage0_groups(project_id))
    rows = (
        Stage0Requirement.objects.filter(project_id=project_id)
        .select_related("checked_by")
        .only(
            "id",
            "key",
            "title",
            "kind",
            "check_target",
            "install_hint",
            "conflicts_with",
            "ignored",
            "status",
            "status_detail",
            "status_source",
            "checked_at",
            "checked_host",
            "origin",
            "checked_by__first_name",
            "checked_by__last_name",
            "checked_by__email",
        )
        .order_by("key")
    )
    return RequirementsPage(
        groups=groups,
        requirements=Paginator(rows, REQUIREMENTS_PER_PAGE).get_page(page),
        unanalysed_count=sum(
            group.case_count for group in groups if not group.analysed
        ),
        analysis_running=analysis_running(project_id),
    )
