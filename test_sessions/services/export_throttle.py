"""Per-user limit on test session CSV exports (each reads up to 50,000 rows)."""

from __future__ import annotations

from django.core.cache import cache

from accounts.models import CustomUser
from test_sessions.exceptions import SessionExportThrottledError

EXPORT_LIMIT = 5
EXPORT_WINDOW_SECONDS = 60


def _throttle_key(user_id: int) -> str:
    return f"test_sessions:session-export:{user_id}"


def check_session_export_throttle(user: CustomUser) -> None:
    """Count one export for ``user``; raise ``SessionExportThrottledError``
    beyond ``EXPORT_LIMIT`` in a fixed ``EXPORT_WINDOW_SECONDS`` window."""
    key = _throttle_key(user.pk)
    cache.add(key, 0, timeout=EXPORT_WINDOW_SECONDS)
    try:
        attempts = cache.incr(key)
    except ValueError:
        # The key expired between add and incr: this is the window's first export.
        cache.add(key, 1, timeout=EXPORT_WINDOW_SECONDS)
        attempts = 1
    if attempts > EXPORT_LIMIT:
        raise SessionExportThrottledError()
