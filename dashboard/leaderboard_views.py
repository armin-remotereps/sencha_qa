from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from accounts.types import AuthenticatedRequest
from dashboard.leaderboard_forms import LeaderboardFilterForm
from dashboard.services.leaderboard import get_leaderboard
from dashboard.services.member_projects import member_project_choices


@login_required
@require_GET
def leaderboard(request: AuthenticatedRequest) -> HttpResponse:
    """Insights > Leaderboard: who tested the most, today or over 7 days."""
    project_choices = member_project_choices(request.user)
    form = LeaderboardFilterForm(project_choices, data=request.GET or None)
    board = get_leaderboard(viewer=request.user, filters=form.filters())
    return render(
        request,
        "dashboard/leaderboard.html",
        {"form": form, "board": board, "active_nav": "insights"},
    )
