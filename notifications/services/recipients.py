"""Which project members get a notification of a given kind."""

from __future__ import annotations

from notifications.models import NotificationPreference
from notifications.services.notification_message import (
    NotificationKind,
    preference_flag,
)


def recipient_user_ids(project_id: int, kind: NotificationKind) -> list[int]:
    """Members of the project with notifications on and ``kind`` opted in.

    One query: preferences joined to the project's member table. A user is a
    member of a project at most once, so no DISTINCT is needed.
    """
    queryset = NotificationPreference.objects.filter(
        user__projects__id=project_id, enabled=True
    )
    flag = preference_flag(kind)
    if flag is not None:
        queryset = queryset.filter(**{flag: True})
    return list(queryset.values_list("user_id", flat=True))
