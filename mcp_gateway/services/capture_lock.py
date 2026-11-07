"""The test machine round-trip guards of ``list_windows`` and
``capture_screenshot``.

- ``machine_round_trip``: at most MCP_MACHINE_ROUND_TRIPS_MAX round trips
  wait at once in this process (each holds a worker thread), refused
  without waiting beyond that.
- ``capture_lock``: one capture per project at a time across processes, a
  Redis key set with NX and an expiry, released by compare-and-delete so a
  lock that expired and was taken by another call is never deleted.

The lock uses its own Redis client on ``REDIS_URL`` (the cache's server)
rather than the Django cache: the cache pickles values, so a Lua script
could not compare the token, and django-redis ships no type information.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

import redis
from django.conf import settings

from mcp_gateway.limits import (
    MCP_CAPTURE_LOCK_REDIS_TIMEOUT_SECONDS,
    MCP_CAPTURE_LOCK_SECONDS,
    MCP_MACHINE_ROUND_TRIPS_MAX,
)
from mcp_gateway.services.capture_errors import CaptureErrorCode, error

logger = logging.getLogger(__name__)

_LOCK_KEY: Final = "mcp:capture-lock:{project_id}"
_LOCK_TOKEN_BYTES: Final = 12
# Deletes the key only while it still holds our token (atomic in Redis).
_RELEASE_SCRIPT: Final = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

_ROUND_TRIPS: Final = threading.BoundedSemaphore(MCP_MACHINE_ROUND_TRIPS_MAX)
# Connections are opened on first use; the pool is shared by the threads.
_REDIS: Final = redis.Redis.from_url(
    settings.REDIS_URL,
    socket_connect_timeout=MCP_CAPTURE_LOCK_REDIS_TIMEOUT_SECONDS,
    socket_timeout=MCP_CAPTURE_LOCK_REDIS_TIMEOUT_SECONDS,
)
_release = _REDIS.register_script(_RELEASE_SCRIPT)


@contextmanager
def machine_round_trip() -> Iterator[None]:
    """Hold one of the process's round-trip slots, or ``[machine_busy]``."""
    if not _ROUND_TRIPS.acquire(blocking=False):
        raise error(CaptureErrorCode.MACHINE_BUSY)
    try:
        yield
    finally:
        _ROUND_TRIPS.release()


@contextmanager
def capture_lock(project_id: int) -> Iterator[None]:
    """Hold the project's capture lock, or ``[machine_busy]``."""
    key = _LOCK_KEY.format(project_id=project_id)
    token = secrets.token_urlsafe(_LOCK_TOKEN_BYTES)
    try:
        taken = _REDIS.set(key, token, nx=True, ex=MCP_CAPTURE_LOCK_SECONDS)
    except redis.RedisError:
        logger.warning("capture lock unavailable project=%s", project_id)
        raise error(CaptureErrorCode.MACHINE_BUSY) from None
    if not taken:
        raise error(CaptureErrorCode.MACHINE_BUSY)
    try:
        yield
    finally:
        # A failed release must not replace the call's outcome; the key
        # expires after MCP_CAPTURE_LOCK_SECONDS on its own.
        try:
            _release(keys=[key], args=[token])
        except redis.RedisError:
            logger.warning("capture lock release failed project=%s", project_id)


def capture_in_progress(project_id: int) -> bool:
    """A capture of the project is waiting for the test machine."""
    try:
        return bool(_REDIS.exists(_LOCK_KEY.format(project_id=project_id)))
    except redis.RedisError:
        # Unknown: answer busy rather than queue a listing behind a capture.
        logger.warning("capture lock unavailable project=%s", project_id)
        return True
