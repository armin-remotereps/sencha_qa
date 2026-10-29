from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse

from accounts.models import CustomUser
from accounts.types import AuthenticatedRequest
from projects.models import Project
from projects.project_access import set_request_project_access
from projects.project_access_services import get_project_access

F = TypeVar("F", bound=Callable[..., HttpResponse])

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _membership_view(view_func: F, *, gate_writes: bool) -> F:
    @wraps(view_func)
    def _wrapped(
        request: AuthenticatedRequest, project_id: int, **kwargs: Any
    ) -> HttpResponse:
        access = get_project_access(project_id, request.user)
        if access is None:
            raise Http404
        if gate_writes and request.method not in _SAFE_METHODS and not access.can_write:
            raise PermissionDenied
        set_request_project_access(request, access)
        return view_func(request, project=access.project, **kwargs)

    return login_required(_wrapped)  # type: ignore[return-value]  # login_required changes signature


def project_membership_required(view_func: F) -> F:
    """Members only; a read-only member gets 403 on any unsafe method."""
    return _membership_view(view_func, gate_writes=True)


def project_read_access_required(view_func: F) -> F:
    """Members only, read-only members included on every method.

    The explicit opt-out of the write gate: use it only for a POST a read-only
    member may make (Leave project).
    """
    return _membership_view(view_func, gate_writes=False)


def get_writable_project(
    project_id: int, user: CustomUser, *, include_archived: bool = False
) -> Project:
    """For views that cannot take the decorator (project list-row actions).

    Same rules as ``project_membership_required`` for a write: 404 for a
    non-member, 403 for a read-only member.
    """
    access = get_project_access(project_id, user, include_archived=include_archived)
    if access is None:
        raise Http404
    if not access.can_write:
        raise PermissionDenied
    return access.project
