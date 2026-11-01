"""The projects the Failures page reads and acts on.

A member sees their non-archived projects and acts (bulk) on the writable
ones. An admin (``has_superuser_project_control``) controls every project, so
they see and act on every non-archived project, member or not.
"""

from __future__ import annotations

from accounts.models import CustomUser
from dashboard.services.member_projects import (
    member_project_choices,
    member_project_choices_and_writable_ids,
)
from projects.models import Project
from projects.project_access import has_superuser_project_control


def _all_project_choices() -> list[tuple[int, str]]:
    """``(id, name)`` of every non-archived project by name (one query)."""
    return list(
        Project.objects.filter(archived=False)
        .order_by("name", "id")
        .values_list("id", "name")
    )


def failure_project_choices(user: CustomUser) -> list[tuple[int, str]]:
    """``(id, name)`` of the projects ``user`` sees on the Failures page."""
    if has_superuser_project_control(user):
        return _all_project_choices()
    return member_project_choices(user)


def failure_project_choices_and_writable_ids(
    user: CustomUser,
) -> tuple[list[tuple[int, str]], list[int]]:
    """``failure_project_choices`` plus the ids bulk actions may touch."""
    if has_superuser_project_control(user):
        choices = _all_project_choices()
        return choices, [project_id for project_id, _ in choices]
    return member_project_choices_and_writable_ids(user)
