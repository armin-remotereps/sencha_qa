"""Use case: exchange a refresh token for a new pair (/token,
``grant_type=refresh_token``).

Refresh tokens rotate: each one is exchanged once and then marked
``rotated_at``. Replaying a rotated token within
``MCP_OAUTH_REFRESH_REUSE_GRACE_SECONDS`` is refused without revoking
(Claude's proactive and reactive refreshes can race); later, it is treated
as theft and the whole grant is revoked (``refresh_reuse``). A grant that
fails the access rule (user inactive, write membership lost, project
archived, Connection key regenerated) is revoked (``membership``).

The previous access tokens of the grant are kept until they expire (at most
``MCP_OAUTH_ACCESS_TTL_SECONDS``; the sweep deletes them): a request already
in flight with the old access token must not fail because a refresh raced
it. Revokes are committed before the error is raised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from django.db import transaction
from django.utils import timezone

from mcp_gateway.exceptions import OAuthGrantRefused
from mcp_gateway.limits import MCP_OAUTH_REFRESH_REUSE_GRACE_SECONDS
from mcp_gateway.models import (
    McpGrant,
    McpOAuthToken,
    OAuthGrantRevokedReason,
    OAuthTokenKind,
)
from mcp_gateway.oauth_policy import MCP_SCOPE
from mcp_gateway.oauth_tokens import REFRESH_TOKEN_PREFIX, hash_token
from mcp_gateway.oauth_types import StoredRefresh, TokenPair
from mcp_gateway.services.oauth_access import fingerprint_matches, live_grants
from mcp_gateway.services.oauth_issue_tokens import issue_token_pair
from mcp_gateway.services.oauth_revoke import revoke_grant

logger = logging.getLogger(__name__)

_TOKEN_MAX_CHARS: Final = 128
_CLIENT_ID_MAX_CHARS: Final = 64
_REFUSED: Final = "The refresh token is not valid."
_NO_ACCESS: Final = "Your access to this project is not valid any more. Reconnect."


@dataclass(frozen=True)
class _Refusal:
    message: str


def _bounded(client_id: str, token: str) -> bool:
    return (
        0 < len(client_id) <= _CLIENT_ID_MAX_CHARS
        and len(token) <= _TOKEN_MAX_CHARS
        and token.startswith(REFRESH_TOKEN_PREFIX)
    )


def load_refresh(client_id: str, token: str) -> StoredRefresh | None:
    """The refresh token ``token`` of ``client_id`` on a grant that is not
    revoked (rotated and expired ones included: ``refresh`` and the SDK
    decide), or None."""
    if not _bounded(client_id, token):
        return None
    row = (
        McpOAuthToken.objects.filter(
            token_hash=hash_token(token),
            kind=OAuthTokenKind.REFRESH,
            grant__client__client_id=client_id,
            grant__revoked_at__isnull=True,
        )
        .values_list("expires_at", "grant__scopes", "grant__resource", "grant__user_id")
        .first()
    )
    if row is None:
        return None
    expires_at, scopes, resource, user_id = row
    return StoredRefresh(
        token=token,
        client_id=client_id,
        scopes=list(scopes),
        expires_at=int(expires_at.timestamp()),
        resource=resource,
        subject=str(user_id),
    )


def _refresh_locked(
    client_id: str, token: str, scopes: list[str], now: datetime
) -> TokenPair | _Refusal:
    row = (
        McpOAuthToken.objects.select_for_update(of=("self",))
        .filter(
            token_hash=hash_token(token),
            kind=OAuthTokenKind.REFRESH,
            grant__client__client_id=client_id,
            grant__revoked_at__isnull=True,
        )
        .only("pk", "grant_id", "expires_at", "rotated_at")
        .first()
    )
    if row is None:
        return _Refusal(_REFUSED)
    if row.rotated_at is not None:
        grace = timedelta(seconds=MCP_OAUTH_REFRESH_REUSE_GRACE_SECONDS)
        if now - row.rotated_at > grace:
            logger.warning("mcp oauth refresh token reused (grant %s)", row.grant_id)
            revoke_grant(row.grant_id, OAuthGrantRevokedReason.REFRESH_REUSE)
        return _Refusal(_REFUSED)
    if row.expires_at <= now:
        return _Refusal("The refresh token has expired.")
    grant = (
        live_grants()
        .filter(pk=row.grant_id)
        .only("pk", "api_key_fingerprint", "project__api_key")
        .first()
    )
    if grant is None or not fingerprint_matches(grant):
        logger.info("mcp oauth grant %s ended: access lost", row.grant_id)
        revoke_grant(row.grant_id, OAuthGrantRevokedReason.MEMBERSHIP)
        return _Refusal(_NO_ACCESS)
    if set(scopes) != {MCP_SCOPE}:
        return _Refusal(f'The only supported scope is "{MCP_SCOPE}".')
    McpOAuthToken.objects.filter(pk=row.pk).update(rotated_at=now)
    McpGrant.objects.filter(pk=row.grant_id).update(last_used_at=now)
    return issue_token_pair(row.grant_id)


def refresh(client_id: str, token: str, scopes: list[str]) -> TokenPair:
    """A new pair for the refresh token. Raises ``OAuthGrantRefused``
    (``invalid_grant``)."""
    if not _bounded(client_id, token):
        raise OAuthGrantRefused(_REFUSED)
    with transaction.atomic():
        outcome = _refresh_locked(client_id, token, scopes, timezone.now())
    if isinstance(outcome, _Refusal):
        raise OAuthGrantRefused(outcome.message)
    return outcome
