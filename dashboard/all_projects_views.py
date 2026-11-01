"""Superuser-only "All projects" page: every project, searchable and filtered."""

from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from accounts.decorators import superuser_required
from accounts.types import AuthenticatedRequest
from auto_tester.pagination import (
    ALLOWED_PER_PAGE,
    elided_page_range,
    parse_page,
    parse_per_page,
)
from dashboard.all_projects_forms import (
    AllProjectsFilterForm,
    encode_all_projects_filters,
)
from dashboard.services.all_projects import list_all_projects
from projects.services import connected_machine_state
from visual_engine.services.pool_status import get_visual_engine_status


@superuser_required
@require_GET
def all_projects(request: AuthenticatedRequest) -> HttpResponse:
    form = AllProjectsFilterForm(request.GET or None)
    filters = form.filters()
    per_page = parse_per_page(request)
    rows = list_all_projects(
        actor=request.user,
        filters=filters,
        page=parse_page(request),
        per_page=per_page,
    )
    filter_query = encode_all_projects_filters(filters)
    # The visual engine is global: one query gives every connected row's state.
    machine_state = connected_machine_state(get_visual_engine_status())
    return render(
        request,
        "dashboard/all_projects.html",
        {
            "active_nav": "all_projects",
            "form": form,
            "rows": rows,
            "page_obj": rows,
            "elided_page_range": elided_page_range(rows),
            "per_page": per_page,
            "allowed_per_page": ALLOWED_PER_PAGE,
            "query_params": f"&per_page={per_page}"
            + (f"&{filter_query}" if filter_query else ""),
            "has_filters": not filters.is_default,
            "total_count": rows.paginator.count,
            "connected_machine_state": machine_state.value,
            # Archive / Restore come back here, filters and page kept.
            "return_path": request.get_full_path(),
        },
    )
