from __future__ import annotations

from django.db.models import OuterRef, Subquery

from accounts.models import CustomUser
from projects.models import Project, ProjectMembership
from projects.project_access import ProjectAccess, has_superuser_project_control


def get_project_access(
    project_id: int, user: CustomUser, *, include_archived: bool = False
) -> ProjectAccess | None:
    """The user's access to the project with their membership role, in one query.

    ``None`` when the project is missing (or archived, unless ``include_archived``)
    or the user is neither a member nor an active superuser.
    """
    projects = Project.objects.filter(pk=project_id).annotate(
        viewer_role=Subquery(
            ProjectMembership.objects.filter(
                project=OuterRef("pk"), user_id=user.pk
            ).values("role")[:1]
        )
    )
    if not include_archived:
        projects = projects.filter(archived=False)
    project = projects.first()
    if project is None:
        return None
    role: str | None = project.viewer_role
    via_superuser = has_superuser_project_control(user)
    if role is None and not via_superuser:
        return None
    return ProjectAccess(project=project, role=role or "", via_superuser=via_superuser)
