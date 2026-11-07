"""The consent page's use cases: load a pending authorization request, then
approve it for one project (mint a code) or deny it.

The handle, the code and the client's ``state`` are never logged. The
redirect URI was validated at registration and at /authorize, so it is safe
to redirect to; the URL is built here with urllib (no MCP SDK import).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from mcp_gateway.exceptions import ConsentProjectRefused, ConsentUnavailable
from mcp_gateway.limits import MCP_OAUTH_CODE_TTL_SECONDS
from mcp_gateway.models import (
    McpAuthorizationCode,
    McpAuthorizationRequest,
    McpOAuthClient,
)
from mcp_gateway.oauth_policy import (
    client_display_name,
    is_loopback_uri,
    redirect_host,
)
from mcp_gateway.oauth_tokens import hash_token, mint_token
from mcp_gateway.services.oauth_access import (
    WritableProject,
    can_approve,
    writable_projects,
)

# A minted handle is 43 characters; anything far longer is not ours and is
# refused before it is hashed.
_HANDLE_MAX_CHARS = 128
_UNAVAILABLE = (
    "This sign-in link has expired or was already used. Start again from the app."
)
_PROJECT_REFUSED = "Choose a project you can edit."


@dataclass(frozen=True)
class ConsentView:
    client_display_name: str
    # The client's self-declared name (untrusted; may be "").
    client_declared_name: str
    redirect_host: str
    # The exact URI the code will be sent to (MCP spec: show it).
    redirect_uri: str
    is_loopback: bool
    projects: list[WritableProject]


def _handle_hash(handle: str) -> str:
    if not handle or len(handle) > _HANDLE_MAX_CHARS:
        raise ConsentUnavailable(_UNAVAILABLE)
    return hash_token(handle)


def load_consent(handle: str, user: CustomUser) -> ConsentView:
    """The pending request behind ``handle`` and the projects ``user`` may
    connect; unknown, consumed or expired → ``ConsentUnavailable``."""
    request = (
        McpAuthorizationRequest.objects.filter(
            handle_hash=_handle_hash(handle),
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .select_related("client")
        .only("redirect_uri", "client__client_name")
        .first()
    )
    if request is None:
        raise ConsentUnavailable(_UNAVAILABLE)
    display_name = client_display_name(request.redirect_uri)
    declared = request.client.client_name.strip()
    return ConsentView(
        client_display_name=display_name,
        client_declared_name=declared,
        redirect_host=redirect_host(request.redirect_uri),
        redirect_uri=request.redirect_uri,
        is_loopback=is_loopback_uri(request.redirect_uri),
        projects=writable_projects(user),
    )


def _lock_pending(handle: str) -> McpAuthorizationRequest:
    """Lock the pending request (inside a transaction) or refuse it."""
    request = (
        McpAuthorizationRequest.objects.select_for_update()
        .filter(handle_hash=_handle_hash(handle))
        .first()
    )
    if (
        request is None
        or request.consumed_at is not None
        or request.expires_at <= timezone.now()
    ):
        raise ConsentUnavailable(_UNAVAILABLE)
    return request


def _redirect_with(redirect_uri: str, params: list[tuple[str, str]]) -> str:
    """``redirect_uri`` with ``params`` appended to its query (existing query
    parameters kept)."""
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(params)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _state_param(request: McpAuthorizationRequest) -> list[tuple[str, str]]:
    return [("state", request.state)] if request.has_state else []


def approve(handle: str, user: CustomUser, project_id: int) -> str:
    """Approve the request for ``project_id``: mint a single-use code and
    return the client redirect URL carrying it (and ``state``)."""
    with transaction.atomic():
        request = _lock_pending(handle)
        if not can_approve(user.pk, project_id):
            raise ConsentProjectRefused(_PROJECT_REFUSED)
        now = timezone.now()
        code = mint_token()
        McpAuthorizationCode.objects.create(
            code_hash=hash_token(code),
            client_id=request.client_id,
            user_id=user.pk,
            project_id=project_id,
            code_challenge=request.code_challenge,
            redirect_uri=request.redirect_uri,
            redirect_uri_provided_explicitly=request.redirect_uri_provided_explicitly,
            resource=request.resource,
            scopes=request.scopes,
            expires_at=now + timedelta(seconds=MCP_OAUTH_CODE_TTL_SECONDS),
        )
        request.consumed_at = now
        request.save(update_fields=["consumed_at"])
        # The sweep keeps a client once it has been issued a code.
        McpOAuthClient.objects.filter(pk=request.client_id).update(last_used_at=now)
        return _redirect_with(
            request.redirect_uri, [("code", code), *_state_param(request)]
        )


def deny(handle: str) -> str:
    """Refuse the request: consume it and return the client redirect URL
    carrying ``error=access_denied`` (and ``state``). Any signed-in user
    holding the handle may refuse it."""
    with transaction.atomic():
        request = _lock_pending(handle)
        request.consumed_at = timezone.now()
        request.save(update_fields=["consumed_at"])
        return _redirect_with(
            request.redirect_uri,
            [("error", "access_denied"), *_state_param(request)],
        )
