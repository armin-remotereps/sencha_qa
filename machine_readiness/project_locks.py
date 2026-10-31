"""Per-project in-flight locks of the scan, the quick check and the analysis.

A lock is a cache entry holding a random token. The request service takes it
with ``cache.add`` and hands the token to its Celery task, which ``claim``s it
again when it starts (the entry may have expired while queued) and
``release``s it in ``finally`` only while it still holds its own token. Every
TTL is short (the work's own time limit plus a margin), so a killed worker
frees the lock soon after. Django's cache has no compare-and-delete; the
get-then-delete window only matters after the TTL expired and someone else
took the lock in between, which the short margins make rare.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from django.core.cache import cache

SCAN_LOCK_PREFIX = "artifact-scan:"
QUICK_CHECK_LOCK_PREFIX = "stage0-quick-check:"
ANALYSIS_LOCK_PREFIX = "stage0-analysis:"


@dataclass(frozen=True)
class ProjectLock:
    key: str
    token: str


def lock_key(prefix: str, project_id: int) -> str:
    return f"{prefix}{project_id}"


def is_locked(prefix: str, project_id: int) -> bool:
    """Whether someone holds the lock (one cache read)."""
    return cache.get(lock_key(prefix, project_id)) is not None


def acquire(prefix: str, project_id: int, *, ttl: int) -> ProjectLock | None:
    """A new lock, or None while another holder has it."""
    lock = ProjectLock(key=lock_key(prefix, project_id), token=secrets.token_hex(8))
    return lock if cache.add(lock.key, lock.token, timeout=ttl) else None


def claim(prefix: str, project_id: int, token: str, *, ttl: int) -> ProjectLock | None:
    """The lock handed over with ``token``, refreshed to ``ttl``.

    Taken anew when it expired meanwhile; None when another holder has it.
    """
    lock = ProjectLock(key=lock_key(prefix, project_id), token=token)
    if cache.get(lock.key) == token or cache.add(lock.key, token, timeout=ttl):
        cache.set(lock.key, token, timeout=ttl)
        return lock
    return None


def refresh(lock: ProjectLock, *, ttl: int) -> bool:
    """Extend a held lock; False when it is no longer ours."""
    if cache.get(lock.key) != lock.token:
        return False
    cache.set(lock.key, lock.token, timeout=ttl)
    return True


def release(lock: ProjectLock) -> None:
    """Free the lock if it still holds our token."""
    if cache.get(lock.key) == lock.token:
        cache.delete(lock.key)
