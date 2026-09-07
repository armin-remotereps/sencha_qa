from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render

from accounts.types import AuthenticatedRequest
from dashboard.services import get_dashboard_data


@login_required
def index(request: AuthenticatedRequest) -> HttpResponse:
    dashboard = get_dashboard_data(request.user)
    return render(
        request,
        "dashboard/index.html",
        {"dashboard": dashboard, "active_nav": "dashboard"},
    )
