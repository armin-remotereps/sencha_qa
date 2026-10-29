from __future__ import annotations

from django.http import HttpRequest

from projects.project_access import get_request_project_access


def project_access(request: HttpRequest) -> dict[str, object]:
    """``project_can_write`` / ``project_role`` for project pages (no query)."""
    access = get_request_project_access(request)
    if access is None:
        return {}
    return {"project_can_write": access.can_write, "project_role": access.role}
