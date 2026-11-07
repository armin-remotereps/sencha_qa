"""Use case: revoke a connection, by the app (RFC 7009 /revoke) or by its
owner on the connections page.

Either way the whole grant is revoked and its tokens deleted (the SDK
recommends revoking the grant on any of its tokens). An unknown token is a
no-op (RFC 7009 §2.2); a grant that is not the user's is ``GrantNotFound``.
"""

from __future__ import annotations

from typing import Final

from django.db import transaction
from django.utils import timezone

from mcp_gateway.exceptions import GrantNotFound
from mcp_gateway.models import McpGrant, McpOAuthToken, OAuthGrantRevokedReason
from mcp_gateway.oauth_tokens import hash_token

_TOKEN_MAX_CHARS: Final = 128
_CLIENT_ID_MAX_CHARS: Final = 64


def revoke_grant(grant_id: int, reason: OAuthGrantRevokedReason) -> None:
    """Revoke ``grant_id`` (the first reason wins) and delete its tokens
    (one UPDATE + one DELETE, atomic). Also used by the code exchange and the
    refresh on replay or lost access."""
    with transaction.atomic():
        McpGrant.objects.filter(pk=grant_id, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason=reason
        )
        McpOAuthToken.objects.filter(grant_id=grant_id).delete()


def revoke_by_token(client_id: str, token: str) -> None:
    """Revoke the grant of ``client_id`` that ``token`` (access or refresh)
    belongs to; nothing when unknown."""
    if not (
        0 < len(client_id) <= _CLIENT_ID_MAX_CHARS
        and 0 < len(token) <= _TOKEN_MAX_CHARS
    ):
        return
    grant_id = (
        McpOAuthToken.objects.filter(
            token_hash=hash_token(token), grant__client__client_id=client_id
        )
        .values_list("grant_id", flat=True)
        .first()
    )
    if grant_id is not None:
        revoke_grant(grant_id, OAuthGrantRevokedReason.CLIENT)


def revoke_grant_for_user(grant_id: int, user_id: int) -> None:
    """Revoke the user's own grant (idempotent). Raises ``GrantNotFound``."""
    if not McpGrant.objects.filter(pk=grant_id, user_id=user_id).exists():
        raise GrantNotFound("This connection does not exist.")
    revoke_grant(grant_id, OAuthGrantRevokedReason.USER)
