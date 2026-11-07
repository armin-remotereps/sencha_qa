"""Plain data passed between the OAuth services and the SDK adapter.

The services never import the MCP SDK; ``mcp_gateway.oauth_provider`` maps
these to and from the SDK's pydantic models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# How a gateway request authenticated (the gateway log line).
AuthKind = Literal["oauth", "key"]


@dataclass(frozen=True)
class StoredClient:
    """A registered client as the SDK needs it back.

    ``metadata`` is the RFC 7591 client information as JSON (the SDK's
    ``OAuthClientInformationFull.model_dump(mode="json")`` minus the secret);
    ``client_secret`` is the decrypted secret, None for a public client.
    """

    metadata: dict[str, object]
    client_secret: str | None


@dataclass(frozen=True)
class AuthorizationInput:
    """A validated /authorize request (the SDK already checked the client,
    the exact redirect URI, ``response_type=code`` and PKCE S256)."""

    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    state: str | None
    scopes: list[str] | None
    resource: str | None


@dataclass(frozen=True)
class IssuedCode:
    """An authorization code the token endpoint may exchange."""

    code: str
    client_id: str
    scopes: list[str]
    # Unix seconds.
    expires_at: float
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    resource: str
    # The approving user's id, as a string (the SDK's ``subject``).
    subject: str


@dataclass(frozen=True)
class StoredRefresh:
    """A refresh token the token endpoint may exchange."""

    token: str
    client_id: str
    scopes: list[str]
    # Unix seconds.
    expires_at: int
    resource: str
    subject: str


@dataclass(frozen=True)
class TokenPair:
    """What /token returns (``token_type`` is always Bearer)."""

    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
