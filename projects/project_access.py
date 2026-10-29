"""A member's resolved access to one project, carried on the request.

The decorator resolves it once; the context processor reads it back so templates
get ``project_can_write`` without another query.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.http import HttpRequest

from projects.models import Project, ProjectRole

_REQUEST_ATTR = "_project_access"


@dataclass(frozen=True)
class ProjectAccess:
    project: Project
    role: str

    @property
    def can_write(self) -> bool:
        return self.role == ProjectRole.MEMBER


def set_request_project_access(request: HttpRequest, access: ProjectAccess) -> None:
    setattr(request, _REQUEST_ATTR, access)


def get_request_project_access(request: HttpRequest) -> ProjectAccess | None:
    access = getattr(request, _REQUEST_ATTR, None)
    return access if isinstance(access, ProjectAccess) else None
