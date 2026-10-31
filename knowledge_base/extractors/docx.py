"""Word (.docx) paragraphs and tables in body order, with python-docx.

Heading and Title styles start sections ("section 'Install the agent'");
tables are tab-separated ("table 2"). The container passes the archive guard
first; python-docx parses with ``resolve_entities=False`` and never fetches
external entities.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

import docx
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph

from knowledge_base.archive_guard import check_ooxml_container
from knowledge_base.chunking import (
    SectionCollector,
    group_by_size,
    quoted,
    range_label,
)
from knowledge_base.exceptions import UnsupportedContent
from knowledge_base.text_limits import CHUNK_TARGET_CHARS

DAMAGED_MESSAGE = "This Word document is damaged or could not be read."
_HEADING_STYLES = ("Heading", "Title")


class _DocxReader:
    def __init__(self, collector: SectionCollector) -> None:
        self._collector = collector
        self._heading = ""
        # (paragraph number, text) of the current section.
        self._paragraphs: list[tuple[int, str]] = []
        self._paragraph_count = 0
        self._table_count = 0

    def read(self, data: bytes) -> None:
        document = docx.Document(io.BytesIO(data))
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                if not self._paragraph(block):
                    return
            elif not self._table(block):
                return
        self._flush()

    def _paragraph(self, paragraph: Paragraph) -> bool:
        self._paragraph_count += 1
        text = paragraph.text.strip()
        if not text:
            return True
        style = paragraph.style
        if style is not None and str(style.name or "").startswith(_HEADING_STYLES):
            if not self._flush():
                return False
            self._heading = " ".join(text.split())
        self._paragraphs.append((self._paragraph_count, text))
        return not self._collector.full

    def _table(self, table: Table) -> bool:
        if not self._flush():
            return False
        self._table_count += 1
        rows = [
            "\t".join(" ".join(cell.text.split()) for cell in row.cells)
            for row in table.rows
        ]
        rows = [row for row in rows if row.strip()]
        name = f"table {self._table_count}"
        groups = list(group_by_size([len(row) + 1 for row in rows], CHUNK_TARGET_CHARS))
        for start, end in groups:
            location = name
            if len(groups) > 1:
                location = f"{name}, {range_label('row', 'rows', start + 1, end)}"
            if not self._collector.add(
                location, self._heading, "\n".join(rows[start:end])
            ):
                return False
        return True

    def _flush(self) -> bool:
        paragraphs = self._paragraphs
        self._paragraphs = []
        if not paragraphs:
            return not self._collector.full
        prefix = f"section {quoted(self._heading)}" if self._heading else ""
        groups = list(
            group_by_size([len(text) + 2 for _, text in paragraphs], CHUNK_TARGET_CHARS)
        )
        for start, end in groups:
            location = _location(prefix, paragraphs, start, end, len(groups))
            body = "\n\n".join(text for _, text in paragraphs[start:end])
            if not self._collector.add(location, self._heading, body):
                return False
        return True


def _location(
    prefix: str,
    paragraphs: Sequence[tuple[int, str]],
    start: int,
    end: int,
    group_count: int,
) -> str:
    if prefix and group_count == 1:
        return prefix
    numbers = range_label(
        "paragraph", "paragraphs", paragraphs[start][0], paragraphs[end - 1][0]
    )
    return f"{prefix}, {numbers}" if prefix else numbers


def extract_docx(data: bytes, collector: SectionCollector) -> None:
    check_ooxml_container(data)
    try:
        _DocxReader(collector).read(data)
    except (PackageNotFoundError, KeyError, ValueError, SyntaxError) as exc:
        # lxml's XMLSyntaxError is a SyntaxError.
        raise UnsupportedContent(DAMAGED_MESSAGE) from exc
