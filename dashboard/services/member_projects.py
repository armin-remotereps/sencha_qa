"""The non-archived projects a user is a member of: the scope of every
cross-project page (dashboard, failures, machine health)."""

from __future__ import annotations

from accounts.models import CustomUser
from projects.models import Project


def member_project_ids(user: CustomUser) -> list[int]:
    """Ids of the user's non-archived projects (one query)."""
    return list(
        Project.objects.filter(members=user, archived=False).values_list(
            "id", flat=True
        )
    )


def member_project_choices(user: CustomUser) -> list[tuple[int, str]]:
    """``(id, name)`` of the user's non-archived projects by name (one query)."""
    return list(
        Project.objects.filter(members=user, archived=False)
        .order_by("name", "id")
        .values_list("id", "name")
    )
