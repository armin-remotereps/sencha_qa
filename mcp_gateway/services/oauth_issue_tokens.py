"""Shared by the code exchange and the refresh: issue a token pair for a
grant.

Internal to the OAuth services (callers hold their own transaction). Only
the sha256 of each token is stored; the plaintext pair is returned once.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from mcp_gateway.limits import (
    MCP_OAUTH_ACCESS_TTL_SECONDS,
    MCP_OAUTH_REFRESH_TTL_SECONDS,
)
from mcp_gateway.models import (
    McpOAuthToken,
    OAuthTokenKind,
)
from mcp_gateway.oauth_policy import MCP_SCOPE
from mcp_gateway.oauth_tokens import (
    ACCESS_TOKEN_PREFIX,
    REFRESH_TOKEN_PREFIX,
    hash_token,
    mint_token,
)
from mcp_gateway.oauth_types import TokenPair


def issue_token_pair(grant_id: int) -> TokenPair:
    """A new access + refresh token for ``grant_id`` (one INSERT)."""
    now = timezone.now()
    access = mint_token(ACCESS_TOKEN_PREFIX)
    refresh = mint_token(REFRESH_TOKEN_PREFIX)
    McpOAuthToken.objects.bulk_create(
        [
            McpOAuthToken(
                token_hash=hash_token(access),
                kind=OAuthTokenKind.ACCESS,
                grant_id=grant_id,
                created_at=now,
                expires_at=now + timedelta(seconds=MCP_OAUTH_ACCESS_TTL_SECONDS),
            ),
            McpOAuthToken(
                token_hash=hash_token(refresh),
                kind=OAuthTokenKind.REFRESH,
                grant_id=grant_id,
                created_at=now,
                expires_at=now + timedelta(seconds=MCP_OAUTH_REFRESH_TTL_SECONDS),
            ),
        ]
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=MCP_OAUTH_ACCESS_TTL_SECONDS,
        scope=MCP_SCOPE,
    )
