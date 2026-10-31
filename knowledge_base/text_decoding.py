"""Decode uploaded text files to ``str`` (pure, no ORM).

UTF-8 (with or without a BOM) first, then UTF-16 when the file starts with
a UTF-16 BOM, then Windows-1252 when the result does not look binary.
Line endings are normalised to LF.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

from knowledge_base.exceptions import ContentMismatch

BINARY_MESSAGE = "This file holds binary data, not text."
CP1252_WARNING = "The file is not UTF-8; it was read as Windows-1252."

# Above this share of control characters a cp1252 decode is treated as binary.
_MAX_CONTROL_RATIO = 0.01
# Control characters that ordinary text files contain (tab, newlines, form
# feed, ANSI escape of coloured logs).
_ALLOWED_CONTROLS = frozenset("\t\n\r\f\x1b")


@dataclass(frozen=True)
class DecodedText:
    text: str
    # "" or a short note shown with the item (e.g. the cp1252 fallback).
    warning: str


def decode_text(data: bytes) -> DecodedText:
    """Decode ``data``; raises ``ContentMismatch`` for binary content."""
    if data.startswith(codecs.BOM_UTF8):
        return DecodedText(_finish(_strict(data, "utf-8-sig")), "")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return DecodedText(_finish(_strict(data, "utf-16")), "")
    if b"\x00" in data:
        raise ContentMismatch(BINARY_MESSAGE)
    try:
        return DecodedText(_finish(data.decode("utf-8")), "")
    except UnicodeDecodeError:
        pass
    # cp1252 leaves five bytes undefined; they become U+FFFD.
    text = data.decode("cp1252", errors="replace")
    if _control_ratio(text) >= _MAX_CONTROL_RATIO:
        raise ContentMismatch(BINARY_MESSAGE)
    return DecodedText(_finish(text), CP1252_WARNING)


def _strict(data: bytes, encoding: str) -> str:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ContentMismatch("This file's text encoding could not be read.") from exc


def _finish(text: str) -> str:
    if "\x00" in text:
        raise ContentMismatch(BINARY_MESSAGE)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _control_ratio(text: str) -> float:
    if not text:
        return 0.0
    controls = sum(
        1 for char in text if ord(char) < 32 and char not in _ALLOWED_CONTROLS
    )
    return controls / len(text)
