"""Everything the dashboard home renders: the attention card and failed widget
(also re-rendered alone by the live refresh, ``dashboard:attention_fragment``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from accounts.models import CustomUser
from dashboard.services.attention import Attention, get_attention
from dashboard.services.failed_case_widget import (
    FailedCaseWidget,
    get_failed_case_widget,
)
from dashboard.services.member_projects import member_and_writable_project_ids


@dataclass(frozen=True)
class DashboardData:
    has_projects: bool
    attention: Attention
    # None when the user has no projects (the page shows the empty state).
    failed_widget: FailedCaseWidget | None


def get_dashboard_data(
    user: CustomUser, *, now: datetime | None = None
) -> DashboardData:
    """The dashboard home for ``user``; a fixed number of queries."""
    current_time = now or timezone.now()
    # Attention covers only writable projects; the failed widget reads all.
    project_ids, writable_ids = member_and_writable_project_ids(user)
    attention = get_attention(user, writable_ids, now=current_time)
    if not project_ids:
        return DashboardData(
            has_projects=False, attention=attention, failed_widget=None
        )
    return DashboardData(
        has_projects=True,
        attention=attention,
        failed_widget=get_failed_case_widget(project_ids, current_time),
    )
