"""Use case: delete OAuth rows nothing needs any more (hourly beat).

Set-based deletes (Django's collector also loads the cascaded rows of grants
and clients, which are few). ``CASCADE`` takes a grant's tokens and a
client's requests, codes and grants with it:

- pending consents and codes past expiry + 1 day;
- tokens past expiry + 1 day;
- grants revoked more than ``MCP_OAUTH_CONNECTIONS_HISTORY_DAYS`` ago, and
  unrevoked grants idle long enough that their refresh token expired that
  long ago (last use older than the refresh lifetime + the history days),
  so an expired connection stays listed on the page as long as a revoked
  one;
- clients that never got an authorization code,
  ``MCP_OAUTH_UNUSED_CLIENT_SECONDS`` after registering (Claude registers a
  new client on every fresh connection);
- clients with no grant left whose last code is more than 90 days old.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from mcp_gateway.limits import (
    MCP_OAUTH_CONNECTIONS_HISTORY_DAYS,
    MCP_OAUTH_REFRESH_TTL_SECONDS,
    MCP_OAUTH_UNUSED_CLIENT_SECONDS,
)
from mcp_gateway.models import (
    McpAuthorizationCode,
    McpAuthorizationRequest,
    McpGrant,
    McpOAuthClient,
    McpOAuthToken,
)

_EXPIRED_GRACE: Final = timedelta(days=1)
_IDLE_CLIENT_AGE: Final = timedelta(days=90)


@dataclass(frozen=True)
class OAuthSweepResult:
    tokens: int
    grants: int
    clients: int


def sweep_mcp_oauth() -> OAuthSweepResult:
    """Delete expired and dead OAuth rows; counts of the main kinds."""
    now = timezone.now()
    expired_before = now - _EXPIRED_GRACE
    history = timedelta(days=MCP_OAUTH_CONNECTIONS_HISTORY_DAYS)
    idle_before = now - timedelta(seconds=MCP_OAUTH_REFRESH_TTL_SECONDS) - history

    McpAuthorizationRequest.objects.filter(expires_at__lt=expired_before).delete()
    McpAuthorizationCode.objects.filter(expires_at__lt=expired_before).delete()
    _, token_counts = McpOAuthToken.objects.filter(
        expires_at__lt=expired_before
    ).delete()

    _, grant_counts = (
        McpGrant.objects.annotate(last_activity=Coalesce("last_used_at", "created_at"))
        .filter(
            Q(revoked_at__lt=now - history)
            | Q(revoked_at__isnull=True, last_activity__lt=idle_before)
        )
        .delete()
    )

    _, client_counts = (
        McpOAuthClient.objects.filter(
            Q(
                last_used_at__isnull=True,
                created_at__lt=now - timedelta(seconds=MCP_OAUTH_UNUSED_CLIENT_SECONDS),
            )
            | Q(last_used_at__lt=now - _IDLE_CLIENT_AGE)
        )
        .filter(~Exists(McpGrant.objects.filter(client=OuterRef("pk"))))
        .delete()
    )
    return OAuthSweepResult(
        tokens=token_counts.get(McpOAuthToken._meta.label, 0),
        grants=grant_counts.get(McpGrant._meta.label, 0),
        clients=client_counts.get(McpOAuthClient._meta.label, 0),
    )
