"""Resolve the app a person or the agent names to an installed app id.

Shared by ``mobile_android`` and ``mobile_ios``: an exact package/bundle id
wins, then a visible name, then a name matching the end of an id.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from selenium.common.exceptions import UnknownMethodException, WebDriverException

from controller_client.exceptions import ExecutionError

_APP_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$"
)
_MAX_LISTED_MATCHES: Final[int] = 5


def looks_like_app_id(app: str) -> bool:
    """``com.example.app``-shaped (a package or bundle id, not a name)."""
    return _APP_ID_RE.match(app.strip()) is not None


def _squash(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def match_app_name(name: str, apps: Mapping[str, str]) -> list[str]:
    """Ids in ``apps`` (id -> visible name, "" when unknown) matching ``name``.

    Exact visible name first, then a visible name or the id's last segment
    equal after dropping spaces and punctuation, then an id containing it.
    """
    wanted = name.strip().lower()
    squashed = _squash(name)
    if not squashed:
        return []
    exact = [app_id for app_id, label in apps.items() if label.lower() == wanted]
    if exact:
        return exact
    close = [
        app_id
        for app_id, label in apps.items()
        if _squash(label) == squashed or _squash(app_id.rsplit(".", 1)[-1]) == squashed
    ]
    if close:
        return close
    return [app_id for app_id in apps if squashed in _squash(app_id)]


def pick_single(name: str, matches: list[str]) -> str:
    """The one match, or ExecutionError saying none or which ones."""
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ExecutionError(
            f"No installed app matches '{name}'. Name it by its package or "
            "bundle id (e.g. com.example.app)."
        )
    listed = ", ".join(sorted(matches)[:_MAX_LISTED_MATCHES])
    raise ExecutionError(
        f"'{name}' matches several installed apps ({listed}); name one by its id."
    )


_UNSUPPORTED_MARKERS: Final[tuple[str, ...]] = (
    "unknown mobile command",
    "unknown command",
    "not supported",
    "unsupported",
    "not implemented",
)


def is_unsupported_command(exc: WebDriverException) -> bool:
    """The driver does not know the command (an older driver or a simulator)."""
    if isinstance(exc, UnknownMethodException):
        return True
    message = (exc.msg or str(exc)).lower()
    return any(marker in message for marker in _UNSUPPORTED_MARKERS)
