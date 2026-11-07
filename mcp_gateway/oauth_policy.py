"""OAuth policy of the MCP gateway (pure: settings only, no ORM, no SDK).

The single scope, the issuer and the canonical resource, which redirect URIs
a client may register, and the trusted display name the consent page leads
with (derived from the redirect host, never from the self-declared name).
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

from django.conf import settings
from pydantic import AnyHttpUrl, ValidationError

MCP_SCOPE: Final = "mcp"
MCP_PATH: Final = "/mcp"
PROTECTED_RESOURCE_PATH: Final = "/.well-known/oauth-protected-resource"

_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_KNOWN_CLIENTS: Final[dict[str, str]] = {
    "chatgpt.com": "ChatGPT",
    "claude.ai": "Claude",
    "claude.com": "Claude",
}
LOOPBACK_DISPLAY_NAME: Final = "An app on your computer"
# The callback paths of the known connector hosts: a code may only go there,
# never to some other page of the same site (an open redirect or a page that
# leaks its URL would hand the code to whoever started the flow). Other
# hosts an operator adds to MCP_OAUTH_REDIRECT_HOSTS accept any path.
_KNOWN_CALLBACK_PATHS: Final[dict[str, re.Pattern[str]]] = {
    "chatgpt.com": re.compile(
        r"/connector_platform_oauth_redirect|/connector/oauth/[A-Za-z0-9_-]{1,128}"
    ),
    "claude.ai": re.compile(r"/api/mcp/auth_callback"),
    "claude.com": re.compile(r"/api/mcp/auth_callback"),
}


def oauth_enabled() -> bool:
    return bool(settings.MCP_OAUTH_ENABLED)


def issuer() -> str:
    """The authorization server issuer: the site origin, no trailing slash."""
    return settings.SITE_URL.strip().rstrip("/")


def canonical_resource() -> str:
    """The protected resource (RFC 8707 / 9728): ``<SITE_URL>/mcp``."""
    return issuer() + MCP_PATH


def resource_metadata_url() -> str:
    """The RFC 9728 metadata URL the 401 points at."""
    return issuer() + PROTECTED_RESOURCE_PATH + MCP_PATH


def _normalised(url: str) -> str | None:
    try:
        return str(AnyHttpUrl(url)).removesuffix("/")
    except ValidationError:
        return None


def resource_matches(value: str) -> bool:
    """``value`` names this server's resource (the SDK's own comparison:
    scheme and host case-insensitive, default port dropped, one trailing
    slash ignored)."""
    given = _normalised(value)
    return given is not None and given == _normalised(canonical_resource())


def _host_of(uri: str) -> tuple[str, str] | None:
    """(scheme, lowercase host) of an absolute URI without a fragment."""
    parsed = _split(uri)
    return (parsed[0], parsed[1]) if parsed else None


def _split(uri: str) -> tuple[str, str, str] | None:
    """(scheme, lowercase host, path) of an absolute URI without a fragment
    or user info."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.fragment or not parts.hostname or parts.username or parts.password:
        return None
    return parts.scheme.lower(), parts.hostname.lower(), parts.path


def is_loopback_uri(uri: str) -> bool:
    parsed = _host_of(uri)
    return parsed is not None and parsed[1] in _LOOPBACK_HOSTS


def redirect_uri_allowed(uri: str) -> bool:
    """A client may register ``uri``: https to an allowlisted host, or http to
    an allowlisted loopback host (exact host; no wildcard; no fragment).
    On a known connector host only its callback path is allowed."""
    parsed = _split(uri)
    if parsed is None:
        return False
    scheme, host, path = parsed
    allowed = {h.strip("[]") for h in settings.MCP_OAUTH_REDIRECT_HOSTS}
    if host not in allowed:
        return False
    if host in _LOOPBACK_HOSTS:
        return scheme == "http"
    if scheme != "https":
        return False
    callback = _KNOWN_CALLBACK_PATHS.get(host)
    return callback is None or callback.fullmatch(path) is not None


def redirect_host(uri: str) -> str:
    """The lowercase host of ``uri`` ("" when it cannot be parsed)."""
    parsed = _host_of(uri)
    return parsed[1] if parsed else ""


def client_display_name(redirect_uri: str) -> str:
    """The trusted name the consent page leads with."""
    host = redirect_host(redirect_uri)
    if host in _LOOPBACK_HOSTS:
        return LOOPBACK_DISPLAY_NAME
    return _KNOWN_CLIENTS.get(host, host)
