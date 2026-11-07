"""Authenticate an MCP request with the project's Connection key.

``ProjectKeyVerifier`` is the SDK ``TokenVerifier`` the bearer middleware
calls on every HTTP request. The key is looked up exactly as the Test Runner
handshake does (``projects.services.get_project_by_api_key``: indexed
equality on the unique ``Project.api_key``, non-archived projects only),
then re-checked in constant time. Regenerating the key stops MCP access at
once, as for runners. The key is never logged, echoed or stored.

Rule: a valid key is always accepted, whatever its client IP has done.
Claude connectors and other hosted clients share egress IPs, so one bad
actor on a shared IP must never lock every project's tester out. Every
non-empty token of a plausible length is therefore looked up (one indexed
equality query; the throttle cannot spare it without refusing valid keys).
Only failed checks are counted, per client IP, up to
``MCP_AUTH_FAILURES_PER_MINUTE`` in a fixed window; once an IP is over the
limit its failures are refused for the rest of the window with no further
counter writes, and a warning is logged when it crosses the limit. The
counter is an alarm and caps the bookkeeping; it is not what stops guessing:
a 256-bit random key cannot be guessed.

The SDK verifier only receives the token, so the client IP comes from
``MCP_CLIENT_IP``, a context variable the ASGI host sets per request
(``scope["client"]``: the real IP behind nginx with ``--proxy-headers``;
nginx overwrites ``X-Forwarded-For`` with the peer address so it cannot be
spoofed). While it is unset nothing is counted.
"""

from __future__ import annotations

import hmac
import logging
from contextvars import ContextVar
from typing import Final

from channels.db import database_sync_to_async
from django.core.cache import cache
from mcp.server.auth.provider import AccessToken

from mcp_gateway.limits import MCP_AUTH_FAILURES_PER_MINUTE
from projects.models import Project
from projects.runner_person_step_services import hit_runner_throttle
from projects.services import get_project_by_api_key

logger = logging.getLogger(__name__)

# The client IP of the current request ("" when the host did not set it).
MCP_CLIENT_IP: ContextVar[str] = ContextVar("mcp_client_ip", default="")

_KEY_MAX_CHARS: Final = Project._meta.get_field("api_key").max_length or 64
_FAILURE_WINDOW_SECONDS: Final = 60


def _failure_key(ip: str) -> str:
    return f"mcp:auth-fail:{ip}"


def _verify(token: str, ip: str) -> int | None:
    """The project id the key names, or None (counts a failure per IP)."""
    project = None
    if token and len(token) <= _KEY_MAX_CHARS:
        project = get_project_by_api_key(token)
    if project is not None and hmac.compare_digest(
        project.api_key.encode(), token.encode()
    ):
        return project.pk
    if ip:
        _count_failure(ip)
    logger.info("mcp auth refused")
    return None


def _count_failure(ip: str) -> None:
    """Count one failed check of ``ip``; no write once it is over the limit."""
    key = _failure_key(ip)
    attempts = cache.get(key)
    if isinstance(attempts, int) and attempts >= MCP_AUTH_FAILURES_PER_MINUTE:
        return
    if not hit_runner_throttle(
        key,
        limit=MCP_AUTH_FAILURES_PER_MINUTE - 1,
        window_seconds=_FAILURE_WINDOW_SECONDS,
    ):
        logger.warning("mcp auth failures over the per-IP limit")


class ProjectKeyVerifier:
    """SDK ``TokenVerifier``: a Connection key → its project's access token."""

    async def verify_token(self, token: str) -> AccessToken | None:
        ip = MCP_CLIENT_IP.get()
        project_id = await database_sync_to_async(_verify, thread_sensitive=False)(
            token.strip(), ip
        )
        if project_id is None:
            return None
        # ``token`` is required by the SDK model; it never leaves the process.
        return AccessToken(token=token, client_id=str(project_id), scopes=[])


def project_id_of(access_token: AccessToken | None) -> int | None:
    """The project id a verified access token carries (None when absent)."""
    if access_token is None:
        return None
    try:
        return int(access_token.client_id)
    except ValueError:
        return None
