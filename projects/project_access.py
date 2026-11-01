"""A user's resolved access to one project, carried on the request.

The decorator resolves it once; the context processor reads it back so templates
get ``project_can_write`` without another query.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.http import HttpRequest

from accounts.models import CustomUser
from projects.models import WRITABLE_ROLES, Project

_REQUEST_ATTR = "_project_access"


def has_superuser_project_control(user: CustomUser) -> bool:
    """The one definition of an admin who controls every project.

    Such a user gets writable access to every project without being a member
    (no notifications, not counted by member invariants).
    """
    return user.is_active and user.is_superuser


@dataclass(frozen=True)
class ProjectAccess:
    project: Project
    # Membership role; "" when the user is not a member.
    role: str
    via_superuser: bool = False

    @property
    def is_member(self) -> bool:
        return self.role != ""

    @property
    def can_write(self) -> bool:
        return self.role in WRITABLE_ROLES or self.via_superuser

    @property
    def is_admin_access(self) -> bool:
        """Writable only through the superuser flag, not a writable membership."""
        return self.via_superuser and self.role not in WRITABLE_ROLES


def set_request_project_access(request: HttpRequest, access: ProjectAccess) -> None:
    setattr(request, _REQUEST_ATTR, access)


def get_request_project_access(request: HttpRequest) -> ProjectAccess | None:
    access = getattr(request, _REQUEST_ATTR, None)
    return access if isinstance(access, ProjectAccess) else None
