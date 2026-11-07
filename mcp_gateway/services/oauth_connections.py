"""The Connected apps page: the user's OAuth connections, newest first.

Active and not-valid connections are listed, plus revoked ones from the last
``MCP_OAUTH_CONNECTIONS_HISTORY_DAYS``. The status is computed on read with
the shared access rule (``oauth_access``), in one query.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from accounts.models import CustomUser
from mcp_gateway.limits import MCP_OAUTH_CONNECTIONS_HISTORY_DAYS
from mcp_gateway.models import (
    McpGrant,
    McpOAuthToken,
    OAuthGrantRevokedReason,
    OAuthTokenKind,
)
from mcp_gateway.oauth_policy import client_display_name
from mcp_gateway.services.oauth_access import fingerprint_matches, writable_membership


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    NOT_VALID = "not_valid"


@dataclass(frozen=True)
class ConnectionRow:
    grant_id: int
    client_display_name: str
    # The client's self-declared name (untrusted; may be "").
    client_declared_name: str
    project_name: str
    created_at: datetime
    last_used_at: datetime | None
    status: ConnectionStatus
    status_label: str


_REVOKED_LABELS: dict[str, str] = {
    OAuthGrantRevokedReason.USER: "Revoked by you",
    OAuthGrantRevokedReason.CLIENT: "Disconnected by the app",
    OAuthGrantRevokedReason.REFRESH_REUSE: "Ended: a sign-in token was reused",
    OAuthGrantRevokedReason.CODE_REUSE: "Ended: a sign-in code was reused",
    OAuthGrantRevokedReason.MEMBERSHIP: "No write access any more",
}


def _status(
    grant: McpGrant, *, has_write: bool, refresh_until: datetime | None, now: datetime
) -> tuple[ConnectionStatus, str]:
    if grant.revoked_at is not None:
        return ConnectionStatus.REVOKED, _REVOKED_LABELS.get(
            grant.revoked_reason, "Revoked"
        )
    if grant.project.archived:
        return ConnectionStatus.NOT_VALID, "Project archived"
    if not has_write:
        return ConnectionStatus.NOT_VALID, "No write access any more"
    if not fingerprint_matches(grant):
        return ConnectionStatus.NOT_VALID, "Connection key regenerated"
    if refresh_until is None or refresh_until <= now:
        return ConnectionStatus.NOT_VALID, "Expired"
    return ConnectionStatus.ACTIVE, "Active"


def list_connections(user: CustomUser) -> list[ConnectionRow]:
    """``user``'s connections, newest first (one query)."""
    now = timezone.now()
    cutoff = now - timedelta(days=MCP_OAUTH_CONNECTIONS_HISTORY_DAYS)
    latest_refresh = (
        McpOAuthToken.objects.filter(
            grant_id=OuterRef("pk"),
            kind=OAuthTokenKind.REFRESH,
            rotated_at__isnull=True,
        )
        .order_by("-expires_at")
        .values("expires_at")[:1]
    )
    grants = (
        McpGrant.objects.filter(user_id=user.pk)
        .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gte=cutoff))
        .select_related("client", "project")
        .only(
            "id",
            "api_key_fingerprint",
            "created_at",
            "last_used_at",
            "revoked_at",
            "revoked_reason",
            "client__client_name",
            "client__redirect_uris",
            "project__name",
            "project__archived",
            "project__api_key",
        )
        .annotate(
            has_write=writable_membership(OuterRef("user_id"), OuterRef("project_id")),
            refresh_until=Subquery(latest_refresh),
        )
        .order_by("-created_at", "-pk")
    )
    rows: list[ConnectionRow] = []
    for grant in grants:
        redirect_uris = grant.client.redirect_uris
        first_uri = (
            redirect_uris[0]
            if isinstance(redirect_uris, list)
            and redirect_uris
            and isinstance(redirect_uris[0], str)
            else ""
        )
        status, label = _status(
            grant,
            has_write=bool(grant.has_write),
            refresh_until=grant.refresh_until,
            now=now,
        )
        rows.append(
            ConnectionRow(
                grant_id=grant.pk,
                client_display_name=client_display_name(first_uri) or "Unknown app",
                client_declared_name=grant.client.client_name.strip(),
                project_name=grant.project.name,
                created_at=grant.created_at,
                last_used_at=grant.last_used_at,
                status=status,
                status_label=label,
            )
        )
    return rows
