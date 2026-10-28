"""Template context shared by every page: the attention badge count."""

from __future__ import annotations

from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from accounts.models import CustomUser
from dashboard.services.attention import count_attention


def attention_badge(request: HttpRequest) -> dict[str, object]:
    """``attention_count`` for the nav badge and the ``(N) `` tab-title prefix.

    Lazy: pages that never render it make no queries. A view may pass its own
    ``attention_count`` (view context wins over context processors) when it
    already knows the number. Authenticated users only.
    """
    user = request.user
    if not isinstance(user, CustomUser) or not user.is_authenticated:
        return {}
    return {"attention_count": SimpleLazyObject(lambda: count_attention(user))}
