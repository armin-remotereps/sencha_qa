"""SSRF guard for Web Push subscription endpoints.

The server POSTs to whatever endpoint a browser hands it, so an endpoint is
accepted only when it is https on the default port, carries no credentials,
and its host is one of ``WEBPUSH_ALLOWED_ENDPOINT_HOSTS`` or a subdomain of
one. Checked when a subscription is saved and again before every send.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.conf import settings

from notifications.exceptions import PushEndpointNotAllowedError


def _allowed_suffixes() -> list[str]:
    return [
        suffix.strip().lower().strip(".")
        for suffix in settings.WEBPUSH_ALLOWED_ENDPOINT_HOSTS
        if suffix.strip().strip(".")
    ]


def _host_allowed(host: str, suffixes: list[str]) -> bool:
    # A label boundary is required: "evilfcm.googleapis.com" must not match
    # "fcm.googleapis.com", only the host itself or "<sub>.fcm.googleapis.com".
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def is_endpoint_allowed(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    if parts.scheme != "https" or port not in (None, 443):
        return False
    if parts.username is not None or parts.password is not None:
        return False
    host = parts.hostname or ""
    # A trailing dot or non-ASCII host would dodge the suffix comparison.
    if not host or host.endswith(".") or not host.isascii():
        return False
    return _host_allowed(host.lower(), _allowed_suffixes())


def ensure_endpoint_allowed(endpoint: str) -> None:
    if not is_endpoint_allowed(endpoint):
        raise PushEndpointNotAllowedError()
