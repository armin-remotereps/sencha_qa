"""Check that a file's content matches its declared kind (pure, no ORM)."""

from __future__ import annotations

import io
import struct
import tarfile
import zipfile

import olefile

from knowledge_base.exceptions import ContentMismatch, EncryptedDocument
from knowledge_base.formats import TEXT_KINDS
from knowledge_base.models import KnowledgeKind
from knowledge_base.text_decoding import decode_text

PDF_MAGIC = b"%PDF-"
PDF_MAGIC_WINDOW = 1024
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
OOXML_CONTENT_TYPES = "[Content_Types].xml"
# An encrypted .docx / .xlsx is an OLE file holding this stream.
OOXML_ENCRYPTED_STREAM = "EncryptedPackage"

_OOXML_MAIN_PART: dict[str, str] = {
    KnowledgeKind.DOCX: "word/document.xml",
    KnowledgeKind.XLSX: "xl/workbook.xml",
}
_OLE_STREAMS: dict[str, tuple[str, ...]] = {
    KnowledgeKind.DOC: ("WordDocument",),
    KnowledgeKind.XLS: ("Workbook", "Book"),
}
_LABELS: dict[str, str] = {
    KnowledgeKind.PDF: "a PDF",
    KnowledgeKind.DOCX: "a Word (.docx) document",
    KnowledgeKind.XLSX: "an Excel (.xlsx) workbook",
    KnowledgeKind.DOC: "a Word 97–2003 (.doc) document",
    KnowledgeKind.XLS: "an Excel 97–2003 (.xls) workbook",
}
_ARCHIVE_LABELS: dict[str, str] = {
    "zip": "a .zip archive",
    "tar": "a .tar or .tar.gz archive",
    "7z": "a .7z archive",
}
_OLE_ERRORS = (OSError, ValueError, IndexError, KeyError, struct.error)


def check_content(kind: str, data: bytes) -> None:
    """Raise ``ContentMismatch`` unless ``data`` looks like ``kind``.

    An encrypted .docx / .xlsx (an OLE ``EncryptedPackage``) raises
    ``EncryptedDocument`` instead, which is the more useful message.
    """
    if kind in TEXT_KINDS:
        decode_text(data)
        return
    if kind == KnowledgeKind.PDF:
        if PDF_MAGIC not in data[:PDF_MAGIC_WINDOW]:
            raise _mismatch(kind, data)
        return
    if kind in _OOXML_MAIN_PART:
        _check_ooxml(kind, data)
        return
    if kind in _OLE_STREAMS:
        if not _ole_has_stream(data, _OLE_STREAMS[kind]):
            raise _mismatch(kind, data)
        return
    if kind == KnowledgeKind.ARCHIVE:
        if not any(_archive_matches(fmt, data) for fmt in _ARCHIVE_LABELS):
            raise ContentMismatch("This file is not a .zip, .tar or .7z archive.")
        return
    raise ContentMismatch("This kind of file is not supported.")


def check_archive_content(fmt: str, data: bytes) -> None:
    """Raise ``ContentMismatch`` unless ``data`` is an archive of ``fmt``."""
    if fmt not in _ARCHIVE_LABELS:
        raise ContentMismatch("This kind of archive is not supported.")
    if not _archive_matches(fmt, data):
        raise ContentMismatch(
            f"The content does not match the file name: it is not "
            f"{_ARCHIVE_LABELS[fmt]}."
        )


def _archive_matches(fmt: str, data: bytes) -> bool:
    if fmt == "zip":
        return data.startswith(ZIP_MAGIC) or zipfile.is_zipfile(io.BytesIO(data))
    if fmt == "7z":
        return data.startswith(SEVEN_ZIP_MAGIC)
    if fmt == "tar":
        try:
            return tarfile.is_tarfile(io.BytesIO(data))
        except (tarfile.TarError, OSError, EOFError, ValueError):
            return False
    return False


def _check_ooxml(kind: str, data: bytes) -> None:
    if data.startswith(OLE_MAGIC):
        if _ole_has_stream(data, (OOXML_ENCRYPTED_STREAM,)):
            raise EncryptedDocument(
                "This document is password protected; upload it without a password."
            )
        raise _mismatch(kind, data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise _mismatch(kind, data) from None
    if OOXML_CONTENT_TYPES not in names or _OOXML_MAIN_PART[kind] not in names:
        raise _mismatch(kind, data)


def _ole_has_stream(data: bytes, streams: tuple[str, ...]) -> bool:
    if not data.startswith(OLE_MAGIC):
        return False
    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            return any(ole.exists(stream) for stream in streams)
    except _OLE_ERRORS:
        return False


def _mismatch(kind: str, data: bytes) -> ContentMismatch:
    expected = _LABELS.get(kind, "this kind of file")
    hint = ""
    if kind == KnowledgeKind.DOC and data.startswith(ZIP_MAGIC):
        hint = " It looks like a .docx; rename it and upload it again."
    elif kind == KnowledgeKind.XLS and data.startswith(ZIP_MAGIC):
        hint = " It looks like a .xlsx; rename it and upload it again."
    elif kind == KnowledgeKind.DOCX and data.startswith(OLE_MAGIC):
        hint = " It looks like a .doc; rename it and upload it again."
    elif kind == KnowledgeKind.XLSX and data.startswith(OLE_MAGIC):
        hint = " It looks like a .xls; rename it and upload it again."
    return ContentMismatch(
        f"The content does not match the file name: it is not {expected}.{hint}"
    )
