from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.types import AuthenticatedRequest
from dashboard.services.dashboard_data import get_attention_fragment, get_dashboard_data


@login_required
@require_GET
def index(request: AuthenticatedRequest) -> HttpResponse:
    dashboard = get_dashboard_data(request.user)
    return render(
        request,
        "dashboard/index.html",
        {
            "dashboard": dashboard,
            "active_nav": "dashboard",
            # Already counted: spares the badge context processor its queries.
            "attention_count": dashboard.attention.total,
        },
    )


@never_cache
@login_required
@require_GET
def attention_fragment(request: AuthenticatedRequest) -> HttpResponse:
    """The "Needs your attention" card alone (HTML), swapped in live."""
    return render(
        request,
        "dashboard/includes/_attention.html",
        {"attention": get_attention_fragment(request.user)},
    )
