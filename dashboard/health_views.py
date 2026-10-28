from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from accounts.types import AuthenticatedRequest
from dashboard.health_forms import MachineHealthFilterForm
from dashboard.services.machine_health import get_machine_health
from dashboard.services.member_projects import member_project_choices


@login_required
@require_GET
def machine_health(request: AuthenticatedRequest) -> HttpResponse:
    """Insights > Machine health: help requests by fault, Night auto-fix
    success and resolutions across the user's projects."""
    project_choices = member_project_choices(request.user)
    form = MachineHealthFilterForm(project_choices, data=request.GET or None)
    project_id, days = form.filters()
    health = get_machine_health(
        [choice_id for choice_id, _ in project_choices], project_id, days
    )
    return render(
        request,
        "dashboard/machine_health.html",
        {"form": form, "health": health, "active_nav": "insights"},
    )
