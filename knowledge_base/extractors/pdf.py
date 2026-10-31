"""PDF text, page by page, with pypdf ("PDF page 4")."""

from __future__ import annotations

import io

from django.conf import settings
from pypdf import PasswordType, PdfReader, apply_configuration, get_configuration
from pypdf.errors import DependencyError, FileNotDecryptedError, PyPdfError

from knowledge_base.chunking import SectionCollector, format_number
from knowledge_base.exceptions import EncryptedDocument, UnsupportedContent

ENCRYPTED_MESSAGE = (
    "This PDF is password protected; remove the password and upload it again."
)
DAMAGED_MESSAGE = "This PDF is damaged or could not be read."
# A page with less text than this is counted as having no text layer.
_MIN_PAGE_CHARS = 20
_MIB = 1024 * 1024
# Tighter than pypdf's defaults (75 MB per stream); text needs far less, and
# no external jbig2dec binary is ever run.
_PDF_LIMITS: dict[str, int | None] = {
    "maximum_declared_stream_length": 32 * _MIB,
    "array_based_stream_maximum_output_length": 32 * _MIB,
    "zlib_maximum_output_length": 32 * _MIB,
    "lzw_maximum_output_length": 32 * _MIB,
    "run_length_maximum_output_length": 32 * _MIB,
    "brotli_maximum_output_length": 32 * _MIB,
    "jbig2_maximum_output_length": 8 * _MIB,
    "image_maximum_buffer_size": 32 * _MIB,
    "stream_decoding_work_maximum_length": 128 * _MIB,
    "jbig2dec_binary": None,
}


def extract_pdf(data: bytes, collector: SectionCollector) -> None:
    with apply_configuration(get_configuration().with_overwrites(**_PDF_LIMITS)):
        try:
            _extract(data, collector)
        except (FileNotDecryptedError, DependencyError) as exc:
            raise EncryptedDocument(ENCRYPTED_MESSAGE) from exc
        except PyPdfError as exc:
            raise UnsupportedContent(DAMAGED_MESSAGE) from exc


def _extract(data: bytes, collector: SectionCollector) -> None:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
        raise EncryptedDocument(ENCRYPTED_MESSAGE)
    page_total = len(reader.pages)
    max_pages: int = settings.KNOWLEDGE_BASE_PDF_MAX_PAGES
    if page_total > max_pages:
        collector.warn(
            f"Only the first {format_number(max_pages)} of "
            f"{format_number(page_total)} pages were read."
        )
    read = min(page_total, max_pages)
    empty = 0
    for index in range(read):
        text = reader.pages[index].extract_text() or ""
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        if len(text.strip()) < _MIN_PAGE_CHARS:
            empty += 1
        if not collector.add(f"PDF page {index + 1}", "", text):
            break
    if empty and empty == read:
        raise UnsupportedContent(
            "This PDF has no text layer (it looks scanned); text recognition "
            "is not supported."
        )
    if empty:
        collector.warn(
            f"{format_number(empty)} of {format_number(read)} pages have no "
            "text (scanned pages are not read)."
        )
