"""Template context shared by every page: the user's open test session."""

from __future__ import annotations

from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from accounts.models import CustomUser
from test_sessions.services.session_queries import get_open_session


def open_test_session(request: HttpRequest) -> dict[str, object]:
    """``open_test_session`` for the nav "Live" chip: the open session or None.

    Lazy: one indexed query, only on pages that render it. ``None`` for
    anonymous users. ``SimpleLazyObject`` proxies ``bool``, so a lazy
    ``None`` is falsy in ``{% if %}``. A view may pass its own value (view
    context wins over context processors).
    """
    user = request.user
    if not isinstance(user, CustomUser) or not user.is_authenticated:
        return {"open_test_session": None}
    return {"open_test_session": SimpleLazyObject(lambda: get_open_session(user))}
