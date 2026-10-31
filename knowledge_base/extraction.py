"""Turn one file's bytes into extracted sections (pure, no ORM).

``extract_sections`` checks the content against the kind (sniffing), runs the
format's extractor and enforces ``KNOWLEDGE_BASE_MAX_ITEM_CHARS`` (the text is
cut there and a warning says so). It is never called for archives, whose
members are read by ``archive_readers``. Expected failures raise
``KnowledgeExtractionError`` subclasses with a safe message; anything else
propagates and the caller reports a generic error.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from knowledge_base.chunking import SectionCollector, format_number
from knowledge_base.exceptions import UnsupportedContent
from knowledge_base.extractors.delimited import extract_delimited
from knowledge_base.extractors.docx import extract_docx
from knowledge_base.extractors.html import extract_html
from knowledge_base.extractors.pdf import extract_pdf
from knowledge_base.extractors.plain import extract_plain
from knowledge_base.extractors.word97 import extract_word97
from knowledge_base.extractors.xls import extract_xls
from knowledge_base.extractors.xlsx import extract_xlsx
from knowledge_base.formats import TEXT_KINDS
from knowledge_base.models import KNOWLEDGE_ERROR_MAX_LENGTH, KnowledgeKind
from knowledge_base.sniffing import check_content
from knowledge_base.text_decoding import decode_text
from knowledge_base.types import ExtractedSection

NO_TEXT_MESSAGE = "No text was found in this file."


@dataclass(frozen=True)
class ExtractionOutput:
    sections: tuple[ExtractedSection, ...]
    char_count: int
    # "" or a short note for the item (fits the item's warning field).
    warning: str


def extract_sections(kind: str, data: bytes) -> ExtractionOutput:
    if kind == KnowledgeKind.ARCHIVE:
        raise ValueError("Archives are expanded, not extracted.")
    max_chars: int = settings.KNOWLEDGE_BASE_MAX_ITEM_CHARS
    collector = SectionCollector(max_chars)
    if kind in TEXT_KINDS:
        # Decoding is the content check of text kinds.
        decoded = decode_text(data)
        collector.warn(decoded.warning)
        if kind == KnowledgeKind.CSV:
            extract_delimited(decoded.text, collector)
        elif kind == KnowledgeKind.HTML:
            extract_html(decoded.text, collector)
        else:
            extract_plain(decoded.text, kind, collector)
    else:
        check_content(kind, data)
        if kind == KnowledgeKind.PDF:
            extract_pdf(data, collector)
        elif kind == KnowledgeKind.DOCX:
            extract_docx(data, collector)
        elif kind == KnowledgeKind.DOC:
            extract_word97(data, collector)
        elif kind == KnowledgeKind.XLSX:
            extract_xlsx(data, collector)
        elif kind == KnowledgeKind.XLS:
            extract_xls(data, collector)
        else:
            raise UnsupportedContent("This kind of file is not supported.")
    if not collector.sections:
        raise UnsupportedContent(NO_TEXT_MESSAGE)
    if collector.truncated:
        collector.warn(
            f"Only the first {format_number(max_chars)} characters were kept."
        )
    return ExtractionOutput(
        sections=tuple(collector.sections),
        char_count=collector.char_count,
        warning=_join_warnings(collector.warnings),
    )


def _join_warnings(warnings: list[str]) -> str:
    text = " ".join(warnings)
    if len(text) <= KNOWLEDGE_ERROR_MAX_LENGTH:
        return text
    return text[: KNOWLEDGE_ERROR_MAX_LENGTH - 1].rstrip() + "…"
