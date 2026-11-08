"""Authenticate an MCP request: an OAuth access token or the Connection key.

``BearerVerifier`` is the SDK ``TokenVerifier`` the bearer middleware calls
on every HTTP request. It accepts two kinds of bearer:

- **OAuth access token** (``pha_`` prefix, only while ``MCP_OAUTH_ENABLED``):
  one query on ``McpOAuthToken`` by sha256 hash (kind access, not expired)
  joined to its grant (not revoked, user active) with an ``EXISTS`` of at
  least one project link passing the shared access rule, Connection key
  fingerprint included (``services.oauth_access.live_links``). The token carries the grant, not a
  project: a grant covers several projects, and each call names its project
  (``call_guard`` resolves it per call). A grant with no live link left is refused here (401, so the
  client re-authenticates). A miss falls through to the key check, so a
  Connection key that happens to start with ``pha_`` still works.
- **Connection key**: looked up exactly as the Test Runner handshake does
  (``projects.services.get_project_by_api_key``: indexed equality on the
  unique ``Project.api_key``, non-archived projects only), then re-checked in
  constant time. The token carries the key's one project. Regenerating the
  key stops MCP access at once, as for runners.

No bearer is ever logged, echoed or stored.

Rule: a valid key is always accepted, whatever its client IP has done.
Claude connectors and other hosted clients share egress IPs, so one bad
actor on a shared IP must never lock every project's tester out. Every
non-empty token of a plausible length is therefore looked up (one indexed
equality query; the throttle cannot spare it without refusing valid keys).
Only failed checks are counted (failed ``pha_`` lookups included, since they
fall through to the key check), per client IP, up to
``MCP_AUTH_FAILURES_PER_MINUTE`` in a fixed window; once an IP is over the
limit its failures are refused for the rest of the window with no further
counter writes, and a warning is logged when it crosses the limit. The
counter is an alarm and caps the bookkeeping; it is not what stops guessing:
a 256-bit random key or token cannot be guessed.

The SDK verifier only receives the token, so the client IP comes from
``MCP_CLIENT_IP``, a context variable the ASGI host sets per request
(``scope["client"]``: the real IP behind nginx with ``--proxy-headers``;
nginx overwrites ``X-Forwarded-For`` with the peer address so it cannot be
spoofed). While it is unset nothing is counted.

Every token carries ``scopes=["mcp"]`` and ``resource`` = the canonical
resource, key tokens included: with OAuth on, the SDK checks both
(``required_scopes`` and ``validate_token_resource``).
"""

from __future__ import annotations

import hmac
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Final

from channels.db import database_sync_to_async
from django.core.cache import cache
from django.db.models import Exists, OuterRef
from django.utils import timezone
from mcp.server.auth.provider import AccessToken

from mcp_gateway.limits import (
    MCP_AUTH_FAILURES_PER_MINUTE,
    MCP_OAUTH_LAST_USED_EVERY_SECONDS,
)
from mcp_gateway.models import McpGrant, McpOAuthToken, OAuthTokenKind
from mcp_gateway.oauth_policy import MCP_SCOPE, canonical_resource, oauth_enabled
from mcp_gateway.oauth_tokens import ACCESS_TOKEN_PREFIX, hash_token
from mcp_gateway.oauth_types import AuthKind, McpCaller
from mcp_gateway.services.oauth_access import live_links
from projects.models import Project
from projects.runner_person_step_services import hit_runner_throttle
from projects.services import get_project_by_api_key

logger = logging.getLogger(__name__)

# The client IP of the current request ("" when the host did not set it).
MCP_CLIENT_IP: ContextVar[str] = ContextVar("mcp_client_ip", default="")

_KEY_MAX_CHARS: Final = Project._meta.get_field("api_key").max_length or 64
_FAILURE_WINDOW_SECONDS: Final = 60


class McpAccessToken(AccessToken):
    """The verified caller of one MCP request (internal; never serialised).

    OAuth: ``grant_id`` and ``user_id`` are set, ``key_project_id`` is None
    (the grant covers several projects; each call names one). Connection
    key: ``key_project_id`` is the key's project, the others are None.
    """

    key_project_id: int | None = None
    user_id: int | None = None
    grant_id: int | None = None


def _failure_key(ip: str) -> str:
    return f"mcp:auth-fail:{ip}"


def _plausible(token: str) -> bool:
    return bool(token) and len(token) <= _KEY_MAX_CHARS


def _load_oauth(token: str) -> McpAccessToken | None:
    """The live access token ``token`` names, or None (one query)."""
    loaded = _load_oauth_with_last_used(token)
    return loaded[0] if loaded else None


def _load_oauth_with_last_used(
    token: str,
) -> tuple[McpAccessToken, datetime | None] | None:
    """The live access token and its grant's ``last_used_at`` (one query)."""
    if not (
        oauth_enabled() and _plausible(token) and token.startswith(ACCESS_TOKEN_PREFIX)
    ):
        return None
    row = (
        McpOAuthToken.objects.filter(
            token_hash=hash_token(token),
            kind=OAuthTokenKind.ACCESS,
            expires_at__gt=timezone.now(),
            grant__revoked_at__isnull=True,
            grant__user__is_active=True,
        )
        # At least one project link passes the access rule (key fingerprint
        # included, in SQL).
        .filter(Exists(live_links().filter(grant_id=OuterRef("grant_id"))))
        .select_related("grant__client")
        .only(
            "expires_at",
            "grant__id",
            "grant__user",
            "grant__last_used_at",
            "grant__client__id",
            "grant__client__client_id",
        )
        .first()
    )
    if row is None:
        return None
    grant = row.grant
    # ``token`` is required by the SDK model; it never leaves the process.
    access = McpAccessToken(
        token=token,
        client_id=grant.client.client_id,
        scopes=[MCP_SCOPE],
        expires_at=int(row.expires_at.timestamp()),
        resource=canonical_resource(),
        subject=str(grant.user_id),
        user_id=grant.user_id,
        grant_id=grant.pk,
    )
    return access, grant.last_used_at


def _mark_grant_used(grant_id: int, last_used_at: datetime | None) -> None:
    """Write ``last_used_at`` only when the loaded value is older than the
    interval (no extra round trip otherwise; two workers may both write)."""
    now = timezone.now()
    every = timedelta(seconds=MCP_OAUTH_LAST_USED_EVERY_SECONDS)
    if last_used_at is None or now - last_used_at >= every:
        McpGrant.objects.filter(pk=grant_id).update(last_used_at=now)


def _verify(token: str, ip: str) -> McpAccessToken | None:
    """The verified caller ``token`` names, or None (counts a failure per IP)."""
    loaded = _load_oauth_with_last_used(token)
    if loaded is not None and loaded[0].grant_id is not None:
        _mark_grant_used(loaded[0].grant_id, loaded[1])
        return loaded[0]
    project = get_project_by_api_key(token) if _plausible(token) else None
    if project is not None and hmac.compare_digest(
        project.api_key.encode(), token.encode()
    ):
        return McpAccessToken(
            token=token,
            client_id=str(project.pk),
            scopes=[MCP_SCOPE],
            resource=canonical_resource(),
            key_project_id=project.pk,
        )
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


def load_oauth_access_token(token: str) -> McpAccessToken | None:
    """The live OAuth access token ``token`` names, or None (sync).

    OAuth only (no Connection key fallback, no failure counting, no
    ``last_used_at`` write): the provider's ``load_access_token``, which the
    SDK's revocation endpoint uses to find the token's client.
    """
    return _load_oauth(token.strip())


class BearerVerifier:
    """SDK ``TokenVerifier``: an OAuth access token or a Connection key →
    the caller's ``McpAccessToken``."""

    async def verify_token(self, token: str) -> AccessToken | None:
        ip = MCP_CLIENT_IP.get()
        return await database_sync_to_async(_verify, thread_sensitive=False)(
            token.strip(), ip
        )


def caller_of(access_token: AccessToken | None) -> McpCaller | None:
    """Who a verified access token names: its OAuth grant or its Connection
    key's project (None when the request had no verified bearer)."""
    if not isinstance(access_token, McpAccessToken):
        return None
    if access_token.grant_id is not None:
        return McpCaller(
            grant_id=access_token.grant_id, key_project_id=None, auth="oauth"
        )
    if access_token.key_project_id is not None:
        return McpCaller(
            grant_id=None, key_project_id=access_token.key_project_id, auth="key"
        )
    return None
