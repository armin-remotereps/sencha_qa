"""Per-user limit on case analysis CSV exports (like the test session export)."""

from __future__ import annotations

from django.core.cache import cache

EXPORT_LIMIT = 5
EXPORT_WINDOW_SECONDS = 60


def _throttle_key(user_id: int) -> str:
    return f"run_triage:csv-export:{user_id}"


def allow_triage_export(user_id: int) -> bool:
    """Count one export; False beyond ``EXPORT_LIMIT`` in a fixed
    ``EXPORT_WINDOW_SECONDS`` window."""
    key = _throttle_key(user_id)
    cache.add(key, 0, timeout=EXPORT_WINDOW_SECONDS)
    try:
        attempts = int(cache.incr(key))
    except ValueError:
        # The key expired between add and incr: the window's first export.
        cache.add(key, 1, timeout=EXPORT_WINDOW_SECONDS)
        attempts = 1
    return attempts <= EXPORT_LIMIT
