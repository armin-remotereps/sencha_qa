"""CSV / TSV by row ranges, with the header row repeated in every section."""

from __future__ import annotations

import csv
import io

from knowledge_base.chunking import SectionCollector, group_by_size, range_label
from knowledge_base.extractors.plain import add_line_ranges
from knowledge_base.text_limits import CHUNK_TARGET_CHARS

CSV_FALLBACK_WARNING = "The rows could not be parsed; the file was read as text."


def extract_delimited(text: str, collector: SectionCollector) -> None:
    delimiter = _guess_delimiter(text)
    try:
        rows = [
            "\t".join(" ".join(cell.split()) for cell in row)
            for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        ]
    except csv.Error:
        # A malformed file or a field above csv's size limit: plain lines.
        collector.warn(CSV_FALLBACK_WARNING)
        add_line_ranges(text.split("\n"), 1, "", "", collector)
        return
    if not rows:
        return
    header = rows[0]
    data = rows[1:]
    if not data:
        collector.add("row 1", "", header)
        return
    budget = max(CHUNK_TARGET_CHARS - len(header) - 1, CHUNK_TARGET_CHARS // 2)
    sizes = [len(row) + 1 for row in data]
    for start, end in group_by_size(sizes, budget):
        # Data rows are numbered after the header row (row 1).
        location = range_label("row", "rows", start + 2, end + 1)
        body = "\n".join([header, *data[start:end]])
        if not collector.add(location, "", body):
            return


def _guess_delimiter(text: str) -> str:
    """Comma unless the header line holds more tabs or semicolons."""
    first_line = text.split("\n", 1)[0]
    return max((",", "\t", ";"), key=first_line.count)
