"""The Connected apps page: the user's OAuth connections, newest first.

Active and not-valid connections are listed, plus revoked ones from the last
``MCP_OAUTH_CONNECTIONS_HISTORY_DAYS``. Each connection lists the projects it
covers with their own status; a connection is active while at least one of
its projects is. Statuses are computed on read with the shared access rule
(``oauth_access``), in two queries (grants, then their project links).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.db.models import OuterRef, Prefetch, Q, Subquery
from django.utils import timezone

from accounts.models import CustomUser
from mcp_gateway.limits import MCP_OAUTH_CONNECTIONS_HISTORY_DAYS
from mcp_gateway.models import (
    McpGrant,
    McpGrantProject,
    McpOAuthClient,
    McpOAuthToken,
    OAuthGrantRevokedReason,
    OAuthTokenKind,
)
from mcp_gateway.oauth_policy import client_names
from mcp_gateway.services.oauth_access import (
    link_fingerprint_matches,
    writable_membership,
)


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    NOT_VALID = "not_valid"


@dataclass(frozen=True)
class ConnectionProjectRow:
    project_name: str
    status: ConnectionStatus
    status_label: str


@dataclass(frozen=True)
class ConnectionRow:
    grant_id: int
    client_display_name: str
    # The client's self-declared name (untrusted; may be "").
    client_declared_name: str
    projects: list[ConnectionProjectRow]
    created_at: datetime
    last_used_at: datetime | None
    status: ConnectionStatus
    status_label: str


_REVOKED_LABELS: dict[str, str] = {
    OAuthGrantRevokedReason.USER: "Revoked by you",
    OAuthGrantRevokedReason.CLIENT: "Disconnected by the app",
    OAuthGrantRevokedReason.REFRESH_REUSE: "Ended: a sign-in token was reused",
    OAuthGrantRevokedReason.CODE_REUSE: "Ended: a sign-in code was reused",
    OAuthGrantRevokedReason.MEMBERSHIP: "Access to the project ended",
    OAuthGrantRevokedReason.REMOVED: "Removed by you",
}


def _revoked_label(reason: str) -> str:
    return _REVOKED_LABELS.get(reason, "Revoked")


def _link_status(
    grant: McpGrant, link: McpGrantProject
) -> tuple[ConnectionStatus, str]:
    if link.revoked_at is not None:
        return ConnectionStatus.REVOKED, _revoked_label(link.revoked_reason)
    if grant.revoked_at is not None:
        return ConnectionStatus.REVOKED, _revoked_label(grant.revoked_reason)
    if link.project.archived:
        return ConnectionStatus.NOT_VALID, "Project archived"
    # ``has_write`` is annotated on the prefetch queryset below; the
    # prefetched rows are typed as plain ``McpGrantProject``.
    if not getattr(link, "has_write", False):
        return ConnectionStatus.NOT_VALID, "No write access any more"
    if not link_fingerprint_matches(link):
        return ConnectionStatus.NOT_VALID, "Connection key regenerated"
    return ConnectionStatus.ACTIVE, "Active"


def _grant_status(
    grant: McpGrant,
    *,
    any_live_link: bool,
    refresh_until: datetime | None,
    now: datetime,
) -> tuple[ConnectionStatus, str]:
    if grant.revoked_at is not None:
        return ConnectionStatus.REVOKED, _revoked_label(grant.revoked_reason)
    if refresh_until is None or refresh_until <= now:
        return ConnectionStatus.NOT_VALID, "Expired"
    if not any_live_link:
        return ConnectionStatus.NOT_VALID, "No project left"
    return ConnectionStatus.ACTIVE, "Active"


def list_connections(user: CustomUser) -> list[ConnectionRow]:
    """``user``'s connections, newest first (two queries)."""
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
    links = (
        McpGrantProject.objects.select_related("project")
        .only(
            "id",
            "grant",
            "api_key_fingerprint",
            "revoked_at",
            "revoked_reason",
            "project__name",
            "project__archived",
            "project__api_key",
        )
        .annotate(
            has_write=writable_membership(
                OuterRef("grant__user_id"), OuterRef("project_id")
            )
        )
        .order_by("project__name", "pk")
    )
    grants = (
        McpGrant.objects.filter(user_id=user.pk)
        .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gte=cutoff))
        .select_related("client")
        .only(
            "id",
            "created_at",
            "last_used_at",
            "revoked_at",
            "revoked_reason",
            "client__client_name",
            "client__redirect_uris",
        )
        .annotate(refresh_until=Subquery(latest_refresh))
        .prefetch_related(Prefetch("projects_links", queryset=links))
        .order_by("-created_at", "-pk")
    )
    rows: list[ConnectionRow] = []
    for grant in grants:
        project_rows: list[ConnectionProjectRow] = []
        for link in grant.projects_links.all():
            link_status, link_label = _link_status(grant, link)
            project_rows.append(
                ConnectionProjectRow(
                    project_name=link.project.name,
                    status=link_status,
                    status_label=link_label,
                )
            )
        status, label = _grant_status(
            grant,
            any_live_link=any(
                p.status == ConnectionStatus.ACTIVE for p in project_rows
            ),
            refresh_until=grant.refresh_until,
            now=now,
        )
        display_name, declared_name = client_names(
            grant.client.client_name, grant.client.redirect_uris
        )
        rows.append(
            ConnectionRow(
                grant_id=grant.pk,
                client_display_name=display_name,
                client_declared_name=declared_name,
                projects=project_rows,
                created_at=grant.created_at,
                last_used_at=grant.last_used_at,
                status=status,
                status_label=label,
            )
        )
    return rows
