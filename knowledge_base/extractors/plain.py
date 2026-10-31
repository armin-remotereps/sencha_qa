"""Text kinds (text, markdown, config, script, source, json, yaml, xml) by lines.

XML is never parsed: it is read as text, so entity tricks cannot expand.
Markdown headings (``#`` .. ``######`` outside code fences) start sections.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from knowledge_base.chunking import (
    SectionCollector,
    group_by_size,
    quoted,
    range_label,
)
from knowledge_base.models import KnowledgeKind
from knowledge_base.text_limits import CHUNK_TARGET_CHARS

_MARKDOWN_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


def extract_plain(text: str, kind: str, collector: SectionCollector) -> None:
    lines = text.split("\n")
    if kind == KnowledgeKind.MARKDOWN:
        _extract_markdown(lines, collector)
    else:
        add_line_ranges(lines, 1, "", "", collector)


def add_line_ranges(
    lines: Sequence[str],
    first_line: int,
    prefix: str,
    heading: str,
    collector: SectionCollector,
) -> bool:
    """Add ``lines`` (numbered from ``first_line``) as "lines a–b" sections.

    ``prefix`` goes before the range in the location ("section 'X', ").
    Returns False once the collector is full.
    """
    sizes = [len(line) + 1 for line in lines]
    for start, end in group_by_size(sizes, CHUNK_TARGET_CHARS):
        location = prefix + range_label(
            "line", "lines", first_line + start, first_line + end - 1
        )
        if not collector.add(location, heading, "\n".join(lines[start:end])):
            return False
    return True


def _extract_markdown(lines: list[str], collector: SectionCollector) -> None:
    heading = ""
    section_start = 0
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _MARKDOWN_HEADING.match(line)
        if match is None:
            continue
        if not _add_markdown_section(lines, section_start, index, heading, collector):
            return
        heading = match.group(2).strip()
        section_start = index
    _add_markdown_section(lines, section_start, len(lines), heading, collector)


def _add_markdown_section(
    lines: list[str],
    start: int,
    end: int,
    heading: str,
    collector: SectionCollector,
) -> bool:
    if start >= end:
        return True
    prefix = f"section {quoted(heading)}, " if heading else ""
    return add_line_ranges(lines[start:end], start + 1, prefix, heading, collector)
