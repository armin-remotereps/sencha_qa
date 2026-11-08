"""Use case: the owner changes which projects a live connection covers
(Connected apps › Edit projects).

Adding a project re-checks write access (``approvable_ids``) and takes the
project's current Connection key fingerprint; a link the connection had
before (removed, or ended by lost access or a regenerated key) is reused.
Removing a project revokes its link with ``REMOVED``. At least one project
must stay: to end the whole connection, the owner uses Revoke.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from mcp_gateway.exceptions import GrantNotFound, GrantProjectsRefused
from mcp_gateway.limits import MCP_OAUTH_GRANT_MAX_PROJECTS
from mcp_gateway.models import McpGrant, McpGrantProject, OAuthGrantRevokedReason
from mcp_gateway.oauth_policy import client_names
from mcp_gateway.oauth_tokens import api_key_fingerprint
from mcp_gateway.services.oauth_access import (
    approvable_ids,
    link_fingerprint_matches,
    live_links,
)
from projects.models import Project

_NOT_FOUND = "This connection does not exist."
_NONE_LEFT = (
    "Choose at least one project. To stop the app on every project, revoke "
    "the connection instead."
)
_TOO_MANY = (
    f"Choose at most {MCP_OAUTH_GRANT_MAX_PROJECTS} projects for one connection."
)
_REFUSED = "You can connect only projects you can edit. Reload the page and try again."


@dataclass(frozen=True)
class GrantProjects:
    grant_id: int
    client_display_name: str
    # The client's self-declared name (untrusted; may be "").
    client_declared_name: str
    # Projects the connection can use now (live links).
    project_ids: list[int]


def load_grant_projects(user: CustomUser, grant_id: int) -> GrantProjects:
    """The user's not-revoked connection ``grant_id`` with the projects it
    can use now (two queries). Raises ``GrantNotFound``."""
    grant = (
        McpGrant.objects.filter(pk=grant_id, user_id=user.pk, revoked_at__isnull=True)
        .select_related("client")
        .only("id", "client__client_name", "client__redirect_uris")
        .first()
    )
    if grant is None:
        raise GrantNotFound(_NOT_FOUND)
    links = live_links().filter(grant_id=grant.pk).order_by("project__name", "pk")
    display_name, declared_name = client_names(
        grant.client.client_name, grant.client.redirect_uris
    )
    return GrantProjects(
        grant_id=grant.pk,
        client_display_name=display_name,
        client_declared_name=declared_name,
        project_ids=[
            link.project_id for link in links if link_fingerprint_matches(link)
        ],
    )


def set_grant_projects(user: CustomUser, grant_id: int, project_ids: list[int]) -> None:
    """Make the user's connection ``grant_id`` cover exactly ``project_ids``.

    Raises ``GrantNotFound`` (unknown, not the user's, or revoked) and
    ``GrantProjectsRefused`` (none, too many, or a project the user cannot
    connect now).
    """
    wanted = set(project_ids)
    if not wanted:
        raise GrantProjectsRefused(_NONE_LEFT)
    if len(wanted) > MCP_OAUTH_GRANT_MAX_PROJECTS:
        raise GrantProjectsRefused(_TOO_MANY)
    now = timezone.now()
    with transaction.atomic():
        grant_pk = (
            McpGrant.objects.select_for_update()
            .filter(pk=grant_id, user_id=user.pk, revoked_at__isnull=True)
            .values_list("pk", flat=True)
            .first()
        )
        if grant_pk is None:
            raise GrantNotFound(_NOT_FOUND)
        existing = {
            link.project_id: link
            for link in McpGrantProject.objects.filter(grant_id=grant_pk)
            .select_related("project")
            .only(
                "id",
                "grant",
                "project__api_key",
                "api_key_fingerprint",
                "revoked_at",
                "revoked_reason",
            )
        }
        dropped = {
            pid
            for pid, link in existing.items()
            if pid not in wanted and link.revoked_at is None
        }
        # One check: every wanted project must pass the rule now (projects
        # already live pass it too); a dropped one that no longer passes it
        # ended on its own, so it keeps that reason rather than "Removed".
        approvable = approvable_ids(user.pk, wanted | dropped)
        if not wanted <= approvable:
            raise GrantProjectsRefused(_REFUSED)

        to_link = {
            pid
            for pid in wanted
            if pid not in existing
            or existing[pid].revoked_at is not None
            or not link_fingerprint_matches(existing[pid])
        }
        fingerprints = _fingerprints(to_link - existing.keys())

        reactivated: list[McpGrantProject] = []
        for pid in to_link & existing.keys():
            link = existing[pid]
            link.api_key_fingerprint = api_key_fingerprint(link.project.api_key)
            link.revoked_at = None
            link.revoked_reason = ""
            reactivated.append(link)
        if reactivated:
            McpGrantProject.objects.bulk_update(
                reactivated, ["api_key_fingerprint", "revoked_at", "revoked_reason"]
            )
        if fingerprints:
            McpGrantProject.objects.bulk_create(
                [
                    McpGrantProject(
                        grant_id=grant_pk,
                        project_id=pid,
                        api_key_fingerprint=fingerprint,
                        created_at=now,
                    )
                    for pid, fingerprint in fingerprints.items()
                ]
            )
        # "Removed by you" only for a link that was still live; one whose
        # access or key had ended keeps that reason.
        removed = {
            pid
            for pid in dropped & approvable
            if link_fingerprint_matches(existing[pid])
        }
        for reason, ids in (
            (OAuthGrantRevokedReason.REMOVED, removed),
            (OAuthGrantRevokedReason.MEMBERSHIP, dropped - removed),
        ):
            if ids:
                McpGrantProject.objects.filter(
                    grant_id=grant_pk, project_id__in=ids, revoked_at__isnull=True
                ).update(revoked_at=now, revoked_reason=reason)


def _fingerprints(project_ids: set[int]) -> dict[int, str]:
    """The current Connection key fingerprint of each new project (one
    query; none when there is nothing to add)."""
    if not project_ids:
        return {}
    return {
        pk: api_key_fingerprint(api_key)
        for pk, api_key in Project.objects.filter(pk__in=project_ids).values_list(
            "pk", "api_key"
        )
    }
