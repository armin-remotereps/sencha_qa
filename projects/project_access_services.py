from __future__ import annotations

from accounts.models import CustomUser
from projects.models import ProjectMembership
from projects.project_access import ProjectAccess


def get_project_access(
    project_id: int, user: CustomUser, *, include_archived: bool = False
) -> ProjectAccess | None:
    """The user's membership of the project with its role, in one query.

    ``None`` when the user is not a member (or the project is archived, unless
    ``include_archived``).
    """
    memberships = ProjectMembership.objects.select_related("project").filter(
        project_id=project_id, user=user
    )
    if not include_archived:
        memberships = memberships.filter(project__archived=False)
    membership = memberships.first()
    if membership is None:
        return None
    return ProjectAccess(project=membership.project, role=membership.role)
