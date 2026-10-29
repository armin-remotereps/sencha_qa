from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse

from accounts.types import AuthenticatedRequest

F = TypeVar("F", bound=Callable[..., HttpResponse])


def superuser_required(view_func: F) -> F:
    """Signed-in active superusers only; anyone else signed in gets 403."""

    @wraps(view_func)
    def _wrapped(request: AuthenticatedRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not (request.user.is_active and request.user.is_superuser):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped)  # type: ignore[return-value]  # login_required changes signature
