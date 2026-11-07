"""Use case: hand a validated /authorize request to the consent page.

The SDK already checked the client, the exact redirect URI, the response
type and PKCE. This service checks the RFC 8707 ``resource`` (absent is
stored as the canonical resource), the scope (only ``mcp``; absent means
``mcp``), the ``state`` length and that the redirect URI is still
allowed, stores a single-use pending request under
the hash of a random handle (``MCP_OAUTH_CONSENT_TTL_SECONDS``) and returns
the absolute URL of the Django consent page for that handle.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from django.urls import reverse
from django.utils import timezone

from mcp_gateway.exceptions import OAuthAuthorizeRefused
from mcp_gateway.limits import MCP_OAUTH_CONSENT_TTL_SECONDS
from mcp_gateway.models import McpAuthorizationRequest, McpOAuthClient
from mcp_gateway.oauth_policy import (
    MCP_SCOPE,
    canonical_resource,
    issuer,
    redirect_uri_allowed,
    resource_matches,
)
from mcp_gateway.oauth_tokens import hash_token, mint_token
from mcp_gateway.oauth_types import AuthorizationInput

_STATE_MAX_CHARS: Final = 512
_URI_MAX_CHARS: Final = 2000
_CHALLENGE_MAX_CHARS: Final = 128
_CLIENT_ID_MAX_CHARS: Final = 64


def start_authorization(request: AuthorizationInput) -> str:
    """Store the pending consent; the consent page URL.

    Raises ``OAuthAuthorizeRefused`` (``invalid_request``, ``invalid_scope``
    or ``invalid_target``).
    """
    if request.resource is None:
        resource = canonical_resource()
    elif len(request.resource) <= _URI_MAX_CHARS and resource_matches(request.resource):
        resource = request.resource
    else:
        raise OAuthAuthorizeRefused(
            "invalid_target", "This server serves only its own MCP resource."
        )
    scopes = [MCP_SCOPE] if request.scopes is None else request.scopes
    if not scopes or any(scope != MCP_SCOPE for scope in scopes):
        raise OAuthAuthorizeRefused(
            "invalid_scope", f'The only supported scope is "{MCP_SCOPE}".'
        )
    if request.state is not None and len(request.state) > _STATE_MAX_CHARS:
        raise OAuthAuthorizeRefused("invalid_request", "The state is too long.")
    if (
        len(request.redirect_uri) > _URI_MAX_CHARS
        or len(request.code_challenge) > _CHALLENGE_MAX_CHARS
    ):
        raise OAuthAuthorizeRefused("invalid_request", "Invalid request.")
    # Re-checked here too: a host removed from MCP_OAUTH_REDIRECT_HOSTS after
    # the client registered must not receive codes any more.
    if not redirect_uri_allowed(request.redirect_uri):
        raise OAuthAuthorizeRefused(
            "invalid_request", "This redirect URI is not allowed any more."
        )

    client_pk = (
        McpOAuthClient.objects.filter(client_id=request.client_id)
        .values_list("pk", flat=True)
        .first()
        if 0 < len(request.client_id) <= _CLIENT_ID_MAX_CHARS
        else None
    )
    if client_pk is None:
        raise OAuthAuthorizeRefused("invalid_request", "Unknown client.")

    handle = mint_token()
    now = timezone.now()
    McpAuthorizationRequest.objects.create(
        handle_hash=hash_token(handle),
        client_id=client_pk,
        redirect_uri=request.redirect_uri,
        redirect_uri_provided_explicitly=request.redirect_uri_provided_explicitly,
        code_challenge=request.code_challenge,
        state=request.state or "",
        has_state=request.state is not None,
        scopes=[MCP_SCOPE],
        resource=resource,
        created_at=now,
        expires_at=now + timedelta(seconds=MCP_OAUTH_CONSENT_TTL_SECONDS),
    )
    return issuer() + reverse("mcp_gateway:consent", kwargs={"handle": handle})
