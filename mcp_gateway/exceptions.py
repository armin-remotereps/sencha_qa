"""Domain errors of the MCP gateway; ``message`` is safe to return to the
MCP client (it never names the key, the project internals or a user)."""

from __future__ import annotations


class McpToolError(Exception):
    """A refused tool call; ``message`` is the tool error text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OAuthError(Exception):
    """Base of the OAuth domain errors; ``message`` is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OAuthRegistrationRefused(OAuthError):
    """A dynamic client registration was refused.

    ``code`` is an RFC 7591 error code (``invalid_redirect_uri`` or
    ``invalid_client_metadata``).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OAuthRegistrationUnavailable(OAuthError):
    """Registration is closed for now (the live client cap is reached)."""


class OAuthAuthorizeRefused(OAuthError):
    """An authorization request was refused.

    ``code`` is an RFC 6749 / 8707 error code (``invalid_request``,
    ``invalid_scope``, ``invalid_target``).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OAuthGrantRefused(OAuthError):
    """A code or refresh token exchange was refused (``invalid_grant``)."""


class ConsentUnavailable(OAuthError):
    """The consent handle is unknown, expired or already used."""


class ConsentProjectRefused(OAuthError):
    """The chosen project is not one the user can approve a connection for."""


class GrantNotFound(OAuthError):
    """The connection does not exist or is not the user's."""
