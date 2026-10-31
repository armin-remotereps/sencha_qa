"""Per-project upload byte budget per hour (Redis cache counter).

Deleting items frees quota at once, but their files stay on disk until the
storage sweep. The budget bounds how many bytes a project can write in that
time, also across parallel requests: bytes are reserved before any file is
written.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache

UPLOAD_BUDGET_WINDOW_SECONDS = 3600


def _key(project_id: int) -> str:
    return f"knowledge_base:upload-bytes:{project_id}"


def reserve_upload_bytes(project_id: int, size: int) -> bool:
    """Count ``size`` bytes against the project's hourly budget; False (and
    nothing counted) when they would exceed it."""
    limit: int = settings.KNOWLEDGE_BASE_UPLOAD_BYTES_PER_HOUR
    key = _key(project_id)
    cache.add(key, 0, timeout=UPLOAD_BUDGET_WINDOW_SECONDS)
    try:
        used = cache.incr(key, size)
    except ValueError:
        # The key expired between add and incr: a new window starts here.
        cache.add(key, size, timeout=UPLOAD_BUDGET_WINDOW_SECONDS)
        used = size
    if used > limit:
        release_upload_bytes(project_id, size)
        return False
    return True


def release_upload_bytes(project_id: int, size: int) -> None:
    """Give back bytes that were reserved but never written."""
    try:
        cache.decr(_key(project_id), size)
    except ValueError:
        # The window expired meanwhile: nothing left to give back.
        pass
