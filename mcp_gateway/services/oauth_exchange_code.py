"""Use case: exchange an authorization code for tokens (/token,
``grant_type=authorization_code``).

``load_code`` gives the SDK the code to check (client, PKCE, redirect URI,
expiry). A code that was already used is a replay: the grant it produced is
revoked (``code_reuse``, RFC 6749 §4.1.2) and the code is refused.

``exchange_code`` locks the code row, refuses a used or expired code,
re-checks the access rule for the approving user and project, creates the
``McpGrant`` with the fingerprint of the project's Connection key, marks the
code used and issues the token pair. Refusals that change state (the replay
revoke, a code spent on a lost access) are committed before the error is
raised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from django.db import transaction
from django.utils import timezone

from mcp_gateway.exceptions import OAuthGrantRefused
from mcp_gateway.models import (
    McpAuthorizationCode,
    McpGrant,
    McpOAuthClient,
    OAuthGrantRevokedReason,
)
from mcp_gateway.oauth_tokens import api_key_fingerprint, hash_token
from mcp_gateway.oauth_types import IssuedCode, TokenPair
from mcp_gateway.services.oauth_access import can_approve
from mcp_gateway.services.oauth_issue_tokens import issue_token_pair
from mcp_gateway.services.oauth_revoke import revoke_grant
from projects.models import Project

logger = logging.getLogger(__name__)

_CODE_MAX_CHARS: Final = 128
_CLIENT_ID_MAX_CHARS: Final = 64
_REFUSED: Final = "The authorization code is not valid."


@dataclass(frozen=True)
class _Refusal:
    message: str


def _bounded(client_id: str, code: str) -> bool:
    return 0 < len(client_id) <= _CLIENT_ID_MAX_CHARS and 0 < len(code) <= (
        _CODE_MAX_CHARS
    )


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
            "project_id",
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
    if not can_approve(row.user_id, row.project_id):
        # Spend the code: a lost access does not come back for this code.
        McpAuthorizationCode.objects.filter(pk=row.pk).update(used_at=now)
        return _Refusal("Your access to this project is not valid any more.")
    api_key = (
        Project.objects.filter(pk=row.project_id)
        .values_list("api_key", flat=True)
        .first()
    )
    if api_key is None:
        return _Refusal(_REFUSED)
    grant = McpGrant.objects.create(
        client_id=row.client_id,
        user_id=row.user_id,
        project_id=row.project_id,
        scopes=list(row.scopes),
        resource=row.resource,
        api_key_fingerprint=api_key_fingerprint(api_key),
        created_at=now,
        last_used_at=now,
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
