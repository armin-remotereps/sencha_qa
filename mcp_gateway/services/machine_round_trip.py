"""The test machine round-trip guards of every MCP tool that reaches the
project's test machine (``list_windows``, ``capture_screenshot`` and the
machine tools inside a claimed case).

- ``machine_round_trip``: at most MCP_MACHINE_ROUND_TRIPS_MAX round trips
  wait at once in this process (each holds a worker thread), refused
  without waiting beyond that.
- ``machine_lock``: one capture or machine action per project at a time
  across processes, a Redis key set with NX and an expiry, released by
  compare-and-delete so a lock that expired and was taken by another call is
  never deleted.
- ``machine_busy``: whether that lock is held right now.

Both guards refuse with ``[machine_busy]``: by default the capture text
(``capture_errors``), so ``list_windows`` and ``capture_screenshot`` answer
exactly as before; the machine tools pass their own ``busy`` error.

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

from mcp_gateway.exceptions import McpToolError
from mcp_gateway.limits import (
    MCP_MACHINE_LOCK_REDIS_TIMEOUT_SECONDS,
    MCP_MACHINE_ROUND_TRIPS_MAX,
)
from mcp_gateway.services.capture_errors import CaptureErrorCode, error

logger = logging.getLogger(__name__)

_LOCK_KEY: Final = "mcp:machine-lock:{project_id}"
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
    socket_connect_timeout=MCP_MACHINE_LOCK_REDIS_TIMEOUT_SECONDS,
    socket_timeout=MCP_MACHINE_LOCK_REDIS_TIMEOUT_SECONDS,
)
_release = _REDIS.register_script(_RELEASE_SCRIPT)


def _busy(busy: McpToolError | None) -> McpToolError:
    return busy if busy is not None else error(CaptureErrorCode.MACHINE_BUSY)


@contextmanager
def machine_round_trip(*, busy: McpToolError | None = None) -> Iterator[None]:
    """Hold one of the process's round-trip slots, or raise ``busy``
    (default: the capture ``[machine_busy]``)."""
    if not _ROUND_TRIPS.acquire(blocking=False):
        raise _busy(busy)
    try:
        yield
    finally:
        _ROUND_TRIPS.release()


@contextmanager
def machine_lock(
    project_id: int, seconds: int, *, busy: McpToolError | None = None
) -> Iterator[None]:
    """Hold the project's machine lock for at most ``seconds``, or raise
    ``busy`` (default: the capture ``[machine_busy]``)."""
    key = _LOCK_KEY.format(project_id=project_id)
    token = secrets.token_urlsafe(_LOCK_TOKEN_BYTES)
    try:
        taken = _REDIS.set(key, token, nx=True, ex=seconds)
    except redis.RedisError:
        logger.warning("machine lock unavailable project=%s", project_id)
        raise _busy(busy) from None
    if not taken:
        raise _busy(busy)
    try:
        yield
    finally:
        # A failed release must not replace the call's outcome; the key
        # expires after ``seconds`` on its own.
        try:
            _release(keys=[key], args=[token])
        except redis.RedisError:
            logger.warning("machine lock release failed project=%s", project_id)


def machine_busy(project_id: int) -> bool:
    """A capture or machine action of the project holds the machine lock."""
    try:
        return bool(_REDIS.exists(_LOCK_KEY.format(project_id=project_id)))
    except redis.RedisError:
        # Unknown: answer busy rather than queue a request behind another.
        logger.warning("machine lock unavailable project=%s", project_id)
        return True
