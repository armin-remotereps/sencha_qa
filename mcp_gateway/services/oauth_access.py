"""The OAuth access rule, shared by consent, code exchange, refresh, the
bearer verifier and the connections page.

A user may approve, and a grant stays valid, only while:

- the user is active;
- the user has a membership with a writable role (``WRITABLE_ROLES``) on the
  project (superusers' implicit admin access does not count; read-only
  members cannot approve: the tools write);
- the project is not archived;
- for a grant: it is not revoked and the project's Connection key still has
  the fingerprint taken at consent (regenerating the key ends every
  connection of the project).

The rule is checked on read every time, so losing write access, archiving
the project or regenerating the key takes effect at the next call.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from django.db.models import Exists, OuterRef, QuerySet

from accounts.models import CustomUser
from mcp_gateway.models import McpGrant
from mcp_gateway.oauth_tokens import api_key_fingerprint
from projects.models import WRITABLE_ROLES, Project, ProjectMembership


@dataclass(frozen=True)
class WritableProject:
    id: int
    name: str


def writable_membership(user_ref: OuterRef, project_ref: OuterRef) -> Exists:
    """``Exists`` of a writable membership of ``user_ref`` on ``project_ref``."""
    return Exists(
        ProjectMembership.objects.filter(
            user=user_ref, project=project_ref, role__in=WRITABLE_ROLES
        )
    )


def writable_projects(user: CustomUser) -> list[WritableProject]:
    """The non-archived projects ``user`` may approve a connection for, by
    name (one query)."""
    if not user.is_active:
        return []
    rows = (
        Project.objects.filter(
            archived=False,
            memberships__user_id=user.pk,
            memberships__role__in=WRITABLE_ROLES,
        )
        .order_by("name", "pk")
        .values_list("pk", "name")
    )
    return [WritableProject(id=pk, name=name) for pk, name in rows]


def can_approve(user_id: int, project_id: int) -> bool:
    """``user_id`` may connect ``project_id`` now (one query)."""
    return Project.objects.filter(
        pk=project_id,
        archived=False,
        memberships__user_id=user_id,
        memberships__user__is_active=True,
        memberships__role__in=WRITABLE_ROLES,
    ).exists()


def live_grants() -> QuerySet[McpGrant]:
    """Grants that are not revoked and pass the access rule, except the key
    fingerprint (compare it with ``fingerprint_matches`` on the loaded row:
    the queryset selects ``project__api_key`` for that)."""
    return (
        McpGrant.objects.filter(
            revoked_at__isnull=True,
            user__is_active=True,
            project__archived=False,
        )
        .filter(writable_membership(OuterRef("user_id"), OuterRef("project_id")))
        .select_related("project")
    )


def fingerprint_matches(grant: McpGrant) -> bool:
    """The project's Connection key is still the one the grant was made
    under (constant-time compare). Needs ``grant.project`` loaded."""
    return hmac.compare_digest(
        api_key_fingerprint(grant.project.api_key), grant.api_key_fingerprint
    )
