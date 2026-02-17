from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse

from accounts.types import AuthenticatedRequest
from projects.services import get_project_for_user

F = TypeVar("F", bound=Callable[..., HttpResponse])


def project_membership_required(
    view_func: F,
) -> F:
    @wraps(view_func)
    def _wrapped(
        request: AuthenticatedRequest, project_id: int, **kwargs: Any
    ) -> HttpResponse:
        user = request.user
        project = get_project_for_user(project_id, user)
        if project is None:
            raise Http404
        return view_func(request, project=project, **kwargs)

    return login_required(_wrapped)  # type: ignore[return-value]  # login_required changes signature
