"""Pure helpers for user-supplied file names (no ORM)."""

from __future__ import annotations

import re

_FILE_NAME_MAX = 120
_FALLBACK_FILE_NAME = "document.md"
_NAME_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


def sanitize_file_name(name: str, *, fallback: str = _FALLBACK_FILE_NAME) -> str:
    """Base name only, no control characters, collapsed whitespace, capped."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _NAME_CONTROLS.sub("", base)
    base = _WHITESPACE.sub(" ", base).strip().lstrip(".")
    if len(base) > _FILE_NAME_MAX:
        stem, dot, extension = base.rpartition(".")
        if dot and 0 < len(extension) < 12:
            base = stem[: _FILE_NAME_MAX - len(extension) - 1] + "." + extension
        else:
            base = base[:_FILE_NAME_MAX]
    return base or fallback
