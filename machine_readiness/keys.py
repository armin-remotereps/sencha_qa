"""The one key format of artifact items and stage-0 requirements (pure)."""

from __future__ import annotations

import re

from machine_readiness.models import KEY_MAX_LENGTH

# Lower-case letters and digits, separated by single "-" or "_".
KEY_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
_NON_KEY_RUN = re.compile(r"[^a-z0-9]+")


def key_error(key: str) -> str | None:
    """Why ``key`` is not a valid key, or None when it is."""
    if not key:
        return "A key is required."
    if len(key) > KEY_MAX_LENGTH:
        return (
            f"Key «{key[:KEY_MAX_LENGTH]}…» is longer than {KEY_MAX_LENGTH} characters."
        )
    if not KEY_RE.fullmatch(key):
        return (
            f"Key «{key}» may hold only lower-case letters and digits, "
            'separated by single "-" or "_".'
        )
    return None


def normalise_key(text: str) -> str:
    """``text`` as a key ("JDK 17" -> "jdk-17"); "" when nothing is left.

    Keeps existing "_" separators; the result may still be too long, so check
    it with ``key_error``.
    """
    parts = [
        _NON_KEY_RUN.sub("-", part).strip("-")
        for part in text.strip().lower().split("_")
    ]
    return "_".join(part for part in parts if part)
