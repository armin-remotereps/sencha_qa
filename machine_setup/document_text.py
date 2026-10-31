"""Pure helpers for documents attached to main chat messages (no ORM)."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from machine_setup.exceptions import SetupDocumentInvalidError

# Characters the agent gets per read_document page.
DOCUMENT_PAGE_CHARS = 12000
DOCUMENT_EXTENSIONS = (".md", ".markdown", ".txt")
# C0 controls other than tab, LF, form feed and CR (CR is normalised away).
_FORBIDDEN_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
NO_TEXT_PLACEHOLDER = "(No message text; see the attached document.)"


@dataclass(frozen=True)
class UploadedDocument:
    """A validated upload, ready to be stored with its USER message."""

    file_name: str
    text: str
    size_bytes: int


@dataclass(frozen=True)
class AttachedDocumentNote:
    """What the agent is told about one attached document."""

    document_id: int
    file_name: str
    char_count: int


def has_document_extension(name: str) -> bool:
    return name.lower().endswith(DOCUMENT_EXTENSIONS)


def decode_document(raw: bytes) -> str:
    """Strict UTF-8 text with LF line endings; raises ``SetupDocumentInvalidError``."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SetupDocumentInvalidError(
            "The document is not UTF-8 text. Attach a Markdown or plain text file."
        ) from exc
    if _FORBIDDEN_CONTROLS.search(text):
        raise SetupDocumentInvalidError(
            "The document contains binary data. Attach a Markdown or plain text file."
        )
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise SetupDocumentInvalidError("The document is empty.")
    return text


def page_count(char_count: int) -> int:
    return max(1, math.ceil(char_count / DOCUMENT_PAGE_CHARS))


def user_turn_content(text: str, documents: Sequence[AttachedDocumentNote]) -> str:
    """The user message the agent sees: the text plus one line per document."""
    lines = [text if text.strip() else NO_TEXT_PLACEHOLDER]
    for document in documents:
        pages = page_count(document.char_count)
        lines.append(
            f'[Attached document id={document.document_id} "{document.file_name}", '
            f"{document.char_count:,} characters, {pages} "
            f"page{'s' if pages != 1 else ''} of {DOCUMENT_PAGE_CHARS:,}. "
            f"Read it with read_document(document_id={document.document_id}, "
            "page=N).]"
        )
    return "\n\n".join(lines)
