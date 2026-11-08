"""Use case: exchange an authorization code for tokens (/token,
``grant_type=authorization_code``).

``load_code`` gives the SDK the code to check (client, PKCE, redirect URI,
expiry). A code that was already used is a replay: the grant it produced is
revoked (``code_reuse``, RFC 6749 §4.1.2) and the code is refused.

``exchange_code`` locks the code row, refuses a used or expired code and
re-checks the access rule for the approving user on every approved project.
When none is still approvable the code is spent and refused; otherwise it
creates the ``McpGrant`` and one ``McpGrantProject`` link per project that is
still approvable (projects lost in the 60 seconds since consent are left
out; the Connected apps page shows the result), each with the fingerprint of
that project's Connection key, marks the code used and issues the token
pair. Refusals that change state (the replay revoke, a code spent on a lost
access) are committed before the error is raised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from django.db import transaction
from django.utils import timezone

from mcp_gateway.exceptions import OAuthGrantRefused
from mcp_gateway.limits import MCP_OAUTH_GRANT_MAX_PROJECTS
from mcp_gateway.models import (
    McpAuthorizationCode,
    McpGrant,
    McpGrantProject,
    McpOAuthClient,
    OAuthGrantRevokedReason,
)
from mcp_gateway.oauth_tokens import api_key_fingerprint, hash_token
from mcp_gateway.oauth_types import IssuedCode, TokenPair
from mcp_gateway.services.oauth_access import approvable_ids
from mcp_gateway.services.oauth_issue_tokens import issue_token_pair
from mcp_gateway.services.oauth_revoke import revoke_grant
from projects.models import Project

logger = logging.getLogger(__name__)

_CODE_MAX_CHARS: Final = 128
_CLIENT_ID_MAX_CHARS: Final = 64
_REFUSED: Final = "The authorization code is not valid."
_NO_ACCESS: Final = "Your access to these projects is not valid any more."


@dataclass(frozen=True)
class _Refusal:
    message: str


def _bounded(client_id: str, code: str) -> bool:
    return 0 < len(client_id) <= _CLIENT_ID_MAX_CHARS and 0 < len(code) <= (
        _CODE_MAX_CHARS
    )


def _code_project_ids(stored: object) -> list[int]:
    """The code's ``project_ids`` JSON as ints (anything else dropped),
    bounded by ``MCP_OAUTH_GRANT_MAX_PROJECTS``."""
    if not isinstance(stored, list):
        return []
    ids = [i for i in stored if isinstance(i, int) and not isinstance(i, bool)]
    return ids[:MCP_OAUTH_GRANT_MAX_PROJECTS]


def _revoke_replayed(code_pk: int, grant_id: int | None) -> None:
    logger.warning("mcp oauth code %s replayed (grant %s)", code_pk, grant_id)
    if grant_id is not None:
        revoke_grant(grant_id, OAuthGrantRevokedReason.CODE_REUSE)


def load_code(client_id: str, code: str) -> IssuedCode | None:
    """The unused code ``code`` of ``client_id`` (expired ones included: the
    SDK checks expiry), or None. A used code revokes the grant it produced."""
    if not _bounded(client_id, code):
        return None
    row = (
        McpAuthorizationCode.objects.filter(
            code_hash=hash_token(code), client__client_id=client_id
        )
        .only(
            "pk",
            "user_id",
            "code_challenge",
            "redirect_uri",
            "redirect_uri_provided_explicitly",
            "resource",
            "scopes",
            "expires_at",
            "used_at",
            "grant_id",
        )
        .first()
    )
    if row is None:
        return None
    if row.used_at is not None:
        _revoke_replayed(row.pk, row.grant_id)
        return None
    return IssuedCode(
        code=code,
        client_id=client_id,
        scopes=list(row.scopes),
        expires_at=row.expires_at.timestamp(),
        code_challenge=row.code_challenge,
        redirect_uri=row.redirect_uri,
        redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
        resource=row.resource,
        subject=str(row.user_id),
    )


def _exchange_locked(client_id: str, code: str, now: datetime) -> TokenPair | _Refusal:
    row = (
        McpAuthorizationCode.objects.select_for_update(of=("self",))
        .filter(code_hash=hash_token(code), client__client_id=client_id)
        .only(
            "pk",
            "client_id",
            "user_id",
            "project_ids",
            "resource",
            "scopes",
            "expires_at",
            "used_at",
            "grant_id",
        )
        .first()
    )
    if row is None:
        return _Refusal(_REFUSED)
    if row.used_at is not None:
        _revoke_replayed(row.pk, row.grant_id)
        return _Refusal(_REFUSED)
    if row.expires_at <= now:
        return _Refusal("The authorization code has expired.")
    approved = approvable_ids(row.user_id, _code_project_ids(row.project_ids))
    keys = (
        list(
            Project.objects.filter(pk__in=approved)
            .order_by("pk")
            .values_list("pk", "api_key")
        )
        if approved
        else []
    )
    if not keys:
        # Spend the code: a lost access does not come back for this code.
        McpAuthorizationCode.objects.filter(pk=row.pk).update(used_at=now)
        return _Refusal(_NO_ACCESS)
    grant = McpGrant.objects.create(
        client_id=row.client_id,
        user_id=row.user_id,
        scopes=list(row.scopes),
        resource=row.resource,
        created_at=now,
        last_used_at=now,
    )
    McpGrantProject.objects.bulk_create(
        McpGrantProject(
            grant=grant,
            project_id=project_id,
            api_key_fingerprint=api_key_fingerprint(api_key),
            created_at=now,
        )
        for project_id, api_key in keys
    )
    McpAuthorizationCode.objects.filter(pk=row.pk).update(used_at=now, grant=grant)
    McpOAuthClient.objects.filter(pk=row.client_id).update(last_used_at=now)
    return issue_token_pair(grant.pk)


def exchange_code(client_id: str, code: str) -> TokenPair:
    """Tokens for the code. Raises ``OAuthGrantRefused`` (``invalid_grant``)."""
    if not _bounded(client_id, code):
        raise OAuthGrantRefused(_REFUSED)
    with transaction.atomic():
        outcome = _exchange_locked(client_id, code, timezone.now())
    if isinstance(outcome, _Refusal):
        raise OAuthGrantRefused(outcome.message)
    return outcome
