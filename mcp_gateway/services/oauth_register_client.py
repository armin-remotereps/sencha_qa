"""Use case: store a dynamically registered OAuth client (RFC 7591).

The SDK's ``RegistrationHandler`` parses the request, mints the client id and
secret and checks the scope against ``valid_scopes``; this service applies
our own rules on top before anything is stored:

- every redirect URI passes ``oauth_policy.redirect_uri_allowed`` (at most
  ``_MAX_REDIRECT_URIS``, each at most ``_URI_MAX_CHARS`` characters);
- the token endpoint auth method is ``none`` (no secret) or one of the two
  secret methods (a secret is required);
- the scope is exactly ``mcp`` (absent is stored as ``mcp``);
- the self-declared name loses its control and format characters and is cut
  to 100 characters (it is untrusted; the consent page leads with the name
  derived from the redirect host);
- at most ``MCP_OAUTH_MAX_CLIENTS`` clients exist at once (the sweep deletes
  clients that got no authorization code within
  ``MCP_OAUTH_UNUSED_CLIENT_SECONDS``).

The secret is stored Fernet-encrypted (the SDK compares plaintext).
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Final

from mcp_gateway.exceptions import (
    OAuthRegistrationRefused,
    OAuthRegistrationUnavailable,
)
from mcp_gateway.limits import MCP_OAUTH_MAX_CLIENTS
from mcp_gateway.models import McpOAuthClient, OAuthTokenEndpointAuthMethod
from mcp_gateway.oauth_policy import MCP_SCOPE, redirect_uri_allowed
from projects.secrets import encrypt_secret

logger = logging.getLogger(__name__)

_MAX_REDIRECT_URIS: Final = 10
_URI_MAX_CHARS: Final = 2000
_CLIENT_ID_MAX_CHARS: Final = 64
_NAME_MAX_CHARS: Final = 100
_SECRET_METHODS: Final = frozenset(
    {
        OAuthTokenEndpointAuthMethod.CLIENT_SECRET_POST.value,
        OAuthTokenEndpointAuthMethod.CLIENT_SECRET_BASIC.value,
    }
)
_METADATA: Final = "invalid_client_metadata"


def _sanitised_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    kept = "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))
    return kept.strip()[:_NAME_MAX_CHARS].strip()


def _redirect_uris(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise OAuthRegistrationRefused(
            "invalid_redirect_uri", "At least one redirect URI is required."
        )
    if len(value) > _MAX_REDIRECT_URIS:
        raise OAuthRegistrationRefused(
            "invalid_redirect_uri",
            f"At most {_MAX_REDIRECT_URIS} redirect URIs may be registered.",
        )
    uris: list[str] = []
    for uri in value:
        if (
            not isinstance(uri, str)
            or len(uri) > _URI_MAX_CHARS
            or not redirect_uri_allowed(uri)
        ):
            raise OAuthRegistrationRefused(
                "invalid_redirect_uri", "This redirect URI is not allowed."
            )
        uris.append(uri)
    return uris


def _auth_method(value: object, client_secret: str | None) -> str:
    if value == OAuthTokenEndpointAuthMethod.NONE.value:
        if client_secret is not None:
            raise OAuthRegistrationRefused(
                _METADATA, "A public client must not have a client secret."
            )
        return OAuthTokenEndpointAuthMethod.NONE.value
    if isinstance(value, str) and value in _SECRET_METHODS:
        if not client_secret:
            raise OAuthRegistrationRefused(
                _METADATA, "This token endpoint auth method needs a client secret."
            )
        return value
    raise OAuthRegistrationRefused(
        _METADATA, "This token endpoint auth method is not supported."
    )


def _check_scope(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or set(value.split()) != {MCP_SCOPE}:
        raise OAuthRegistrationRefused(
            _METADATA, f'The only supported scope is "{MCP_SCOPE}".'
        )


def _secret_expires_at(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


def register_client(metadata: dict[str, object], client_secret: str | None) -> None:
    """Validate and store a client the SDK registered.

    Raises ``OAuthRegistrationRefused`` (``invalid_redirect_uri`` or
    ``invalid_client_metadata``) or ``OAuthRegistrationUnavailable`` (cap).
    """
    client_id = metadata.get("client_id")
    if not isinstance(client_id, str) or not 0 < len(client_id) <= (
        _CLIENT_ID_MAX_CHARS
    ):
        raise OAuthRegistrationRefused(_METADATA, "Invalid client id.")
    redirect_uris = _redirect_uris(metadata.get("redirect_uris"))
    auth_method = _auth_method(
        metadata.get("token_endpoint_auth_method"), client_secret
    )
    _check_scope(metadata.get("scope"))
    name = _sanitised_name(metadata.get("client_name"))

    if McpOAuthClient.objects.count() >= MCP_OAUTH_MAX_CLIENTS:
        logger.warning(
            "mcp oauth registration refused: %s clients reached",
            MCP_OAUTH_MAX_CLIENTS,
        )
        raise OAuthRegistrationUnavailable(
            "Registration is closed for now. Try again later."
        )

    stored = {
        **metadata,
        "client_name": name or None,
        "scope": MCP_SCOPE,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": auth_method,
    }
    stored.pop("client_secret", None)
    McpOAuthClient.objects.create(
        client_id=client_id,
        client_name=name,
        redirect_uris=redirect_uris,
        token_endpoint_auth_method=auth_method,
        client_secret_encrypted=(
            encrypt_secret(client_secret)
            if auth_method != OAuthTokenEndpointAuthMethod.NONE.value and client_secret
            else ""
        ),
        client_secret_expires_at=_secret_expires_at(
            metadata.get("client_secret_expires_at")
        ),
        scope=MCP_SCOPE,
        metadata=stored,
    )
