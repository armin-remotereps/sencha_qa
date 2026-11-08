"""The OAuth access rule, shared by consent, code exchange, refresh, the
bearer verifier and the connections page.

A user may approve, and a grant stays valid, only while:

- the user is active;
- the user has a membership with a writable role (``WRITABLE_ROLES``) on the
  project (superusers' implicit admin access does not count; read-only
  members cannot approve: the tools write);
- the project is not archived;
- for a connection: the grant and its link to the project are not revoked,
  and the project's Connection key still has the fingerprint taken when the
  link was made (regenerating the key ends every connection's link to the
  project). A grant covers several projects; each link is checked on its
  own.

The rule is checked on read every time, so losing write access, archiving
the project or regenerating the key takes effect at the next call.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from dataclasses import dataclass

from django.db.models import CharField, Exists, Func, OuterRef, QuerySet

from accounts.models import CustomUser
from mcp_gateway.models import McpGrantProject
from mcp_gateway.oauth_tokens import api_key_fingerprint
from projects.models import WRITABLE_ROLES, Project, ProjectMembership


@dataclass(frozen=True)
class WritableProject:
    id: int
    name: str


class _Sha256Hex(Func):
    """Lowercase sha256 hex of a text column, as ``api_key_fingerprint``
    computes it. PostgreSQL's built-in ``sha256(bytea)``, not Django's
    ``SHA256`` (that one needs the pgcrypto extension)."""

    template = "ENCODE(SHA256(CONVERT_TO(%(expressions)s, 'UTF8')), 'hex')"
    output_field = CharField()


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


def approvable_ids(user_id: int, project_ids: Iterable[int]) -> set[int]:
    """The ids among ``project_ids`` that ``user_id`` may connect now (one
    query)."""
    ids = set(project_ids)
    if not ids:
        return set()
    return set(
        Project.objects.filter(
            pk__in=ids,
            archived=False,
            memberships__user_id=user_id,
            memberships__user__is_active=True,
            memberships__role__in=WRITABLE_ROLES,
        ).values_list("pk", flat=True)
    )


def live_links() -> QuerySet[McpGrantProject]:
    """Project links that are not revoked, whose grant is not revoked, and
    that pass the access rule, the key fingerprint included (sha256 of the
    project's key in SQL, so an ``Exists`` of this queryset is the whole
    rule). Callers that load rows still compare with
    ``link_fingerprint_matches`` (constant time); the queryset selects the
    project for that."""
    return (
        McpGrantProject.objects.filter(
            revoked_at__isnull=True,
            grant__revoked_at__isnull=True,
            grant__user__is_active=True,
            project__archived=False,
            api_key_fingerprint=_Sha256Hex("project__api_key"),
        )
        .filter(writable_membership(OuterRef("grant__user_id"), OuterRef("project_id")))
        .select_related("project")
    )


def link_fingerprint_matches(link: McpGrantProject) -> bool:
    """The project's Connection key is still the one the link was made
    under (constant-time compare). Needs ``link.project`` loaded."""
    return hmac.compare_digest(
        api_key_fingerprint(link.project.api_key), link.api_key_fingerprint
    )
