"""Whether ntfy is configured, and the links that subscribe to a topic.

Setting values are never logged: an invalid value is reported by name only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlencode, urlsplit

from django.conf import settings

from notifications.exceptions import NotificationsNotConfiguredError

logger = logging.getLogger(__name__)

# Prefix + the 32-character random part must fit ntfy's 64-character topic limit.
TOPIC_PREFIX_PATTERN = re.compile(r"[-_A-Za-z0-9]{0,16}")
# Name the ntfy Android app shows for the subscription.
SUBSCRIPTION_DISPLAY_NAME = "Punk Hazard"


@dataclass(frozen=True)
class NtfySubscribeLinks:
    # ntfy://host/topic?display=... : opens the Android app's subscribe dialog.
    app_url: str
    # <server>/<topic>: the ntfy web app, where a desktop browser can subscribe.
    web_url: str
    # The server to add by hand in the iOS or desktop app.
    server_url: str


def _server_url_setting() -> str:
    return str(settings.NTFY_SERVER_URL).strip().rstrip("/")


@lru_cache(maxsize=8)
def _check_config(server_url: str, topic_prefix: str, has_token: bool) -> bool:
    # Cached per value combination, so each warning is logged once per process.
    if not server_url:
        return False
    try:
        parts = urlsplit(server_url)
    except ValueError:
        logger.warning("NTFY_SERVER_URL is not a valid URL; notifications are off")
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        logger.warning("NTFY_SERVER_URL must be an http(s) URL; notifications are off")
        return False
    if parts.username is not None or parts.password is not None:
        logger.warning(
            "NTFY_SERVER_URL must not carry credentials (use NTFY_ACCESS_TOKEN); "
            "notifications are off"
        )
        return False
    if TOPIC_PREFIX_PATTERN.fullmatch(topic_prefix) is None:
        logger.warning(
            "NTFY_TOPIC_PREFIX must be at most 16 characters of [-_A-Za-z0-9]; "
            "notifications are off"
        )
        return False
    if has_token and parts.scheme != "https":
        logger.warning(
            "NTFY_ACCESS_TOKEN is set but NTFY_SERVER_URL is not https: "
            "the token is sent unencrypted"
        )
    return True


def ntfy_configured() -> bool:
    """True when NTFY_SERVER_URL is a valid http(s) URL and the prefix is valid."""
    return _check_config(
        _server_url_setting(),
        str(settings.NTFY_TOPIC_PREFIX),
        bool(settings.NTFY_ACCESS_TOKEN),
    )


def ensure_ntfy_configured() -> None:
    if not ntfy_configured():
        raise NotificationsNotConfiguredError()


def ntfy_server_url() -> str:
    """The configured server URL without a trailing slash ("" when unset)."""
    return _server_url_setting()


def ntfy_subscribe_links(topic: str) -> NtfySubscribeLinks:
    """Links that subscribe to ``topic`` on the configured server.

    ``topic`` uses ntfy's URL-safe alphabet, so it is placed in paths as is.
    """
    server_url = ntfy_server_url()
    parts = urlsplit(server_url)
    query: dict[str, str] = {"display": SUBSCRIPTION_DISPLAY_NAME}
    if parts.scheme == "http":
        query["secure"] = "false"
    host_and_path = parts.netloc + parts.path.rstrip("/")
    return NtfySubscribeLinks(
        app_url=f"ntfy://{host_and_path}/{topic}?{urlencode(query)}",
        web_url=f"{server_url}/{topic}",
        server_url=server_url,
    )
