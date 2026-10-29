"""The non-archived projects a user is a member of: the scope of every
cross-project page (dashboard, failures, machine health).

Reads (failures, machine health) use every member project; attention and
cross-project writes (failures bulk) use only the writable ones.
"""

from __future__ import annotations

from accounts.models import CustomUser
from projects.models import Project, ProjectMembership, ProjectRole


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


def writable_member_project_ids(user: CustomUser) -> list[int]:
    """Ids of the user's non-archived projects where they may write (one query)."""
    return list(
        ProjectMembership.objects.filter(
            user=user, role=ProjectRole.MEMBER, project__archived=False
        ).values_list("project_id", flat=True)
    )


def member_and_writable_project_ids(
    user: CustomUser,
) -> tuple[list[int], list[int]]:
    """``(all, writable)`` ids of the user's non-archived projects (one query).

    For pages that read every member project but scope attention to the
    writable ones, without a second query.
    """
    rows = ProjectMembership.objects.filter(
        user=user, project__archived=False
    ).values_list("project_id", "role")
    all_ids: list[int] = []
    writable_ids: list[int] = []
    for project_id, role in rows:
        all_ids.append(project_id)
        if role == ProjectRole.MEMBER:
            writable_ids.append(project_id)
    return all_ids, writable_ids


def member_project_choices_and_writable_ids(
    user: CustomUser,
) -> tuple[list[tuple[int, str]], list[int]]:
    """``member_project_choices`` plus the writable ids among them (one query).

    For cross-project writes (failures bulk): filters keep every member
    project, the action only touches the writable ones.
    """
    rows = (
        ProjectMembership.objects.filter(user=user, project__archived=False)
        .order_by("project__name", "project_id")
        .values_list("project_id", "project__name", "role")
    )
    choices: list[tuple[int, str]] = []
    writable_ids: list[int] = []
    for project_id, name, role in rows:
        choices.append((project_id, name))
        if role == ProjectRole.MEMBER:
            writable_ids.append(project_id)
    return choices, writable_ids
