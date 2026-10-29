"""Which project members get a notification of a given kind."""

from __future__ import annotations

from notifications.models import NotificationPreference
from notifications.services.notification_message import (
    NotificationKind,
    preference_flag,
)
from projects.models import WRITABLE_ROLES


def recipient_user_ids(
    project_id: int, kind: NotificationKind, *, among: list[int] | None = None
) -> list[int]:
    """Writable members of the project with notifications on and ``kind`` opted in.

    ``among`` narrows the answer to those users (e.g. the one person a chat
    reply is for). Read-only members get nothing. One query: preferences
    joined to the project's membership row (project and role in one
    ``filter()`` so both hit the same row). A user is a member of a project
    at most once, so no DISTINCT is needed.
    """
    queryset = NotificationPreference.objects.filter(
        user__project_memberships__project_id=project_id,
        user__project_memberships__role__in=WRITABLE_ROLES,
        enabled=True,
    )
    if among is not None:
        queryset = queryset.filter(user_id__in=among)
    flag = preference_flag(kind)
    if flag is not None:
        queryset = queryset.filter(**{flag: True})
    return list(queryset.values_list("user_id", flat=True))
