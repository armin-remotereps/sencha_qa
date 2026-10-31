"""Collect extracted sections and cut them into chunks and read pages (pure).

Extractors add sections to a ``SectionCollector`` (which enforces the item's
character budget); ``chunk_sections`` then cuts them into chunks of about
``CHUNK_TARGET_CHARS`` (never more than ``CHUNK_MAX_CHARS``) and packs whole
chunks into read pages of at most ``KNOWLEDGE_PAGE_CHARS``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from knowledge_base.models import (
    KNOWLEDGE_HEADING_MAX_LENGTH,
    KNOWLEDGE_LOCATION_MAX_LENGTH,
)
from knowledge_base.text_limits import (
    CHUNK_MAX_CHARS,
    CHUNK_TARGET_CHARS,
    KNOWLEDGE_PAGE_CHARS,
)
from knowledge_base.types import ExtractedSection

ELLIPSIS = "…"
# Where a chunk is preferably cut, best first.
_SEPARATORS = ("\n\n", "\n", " ")


@dataclass(frozen=True)
class ChunkDraft:
    # 0-based position of the chunk in the item.
    ordinal: int
    # 1-based read page.
    page: int
    location: str
    heading: str
    text: str


class SectionCollector:
    """Accumulates an item's sections within a character budget."""

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self.sections: list[ExtractedSection] = []
        self.char_count = 0
        # True once text was dropped because the budget ran out.
        self.truncated = False
        self.warnings: list[str] = []

    @property
    def full(self) -> bool:
        return self.char_count >= self._max_chars

    def add(self, location: str, heading: str, text: str) -> bool:
        """Add a section; False once the budget is used up (stop extracting)."""
        text = text.strip("\n")
        if not text.strip():
            return not self.full
        if self.full:
            self.truncated = True
            return False
        room = self._max_chars - self.char_count
        if len(text) > room:
            text = text[:room]
            self.truncated = True
        self.sections.append(
            ExtractedSection(
                location=clip(location, KNOWLEDGE_LOCATION_MAX_LENGTH),
                heading=clip(heading, KNOWLEDGE_HEADING_MAX_LENGTH),
                text=text,
            )
        )
        self.char_count += len(text)
        return not self.full

    def warn(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)


def clip(text: str, max_length: int) -> str:
    """``text`` on one line, cut to ``max_length`` with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + ELLIPSIS


def format_number(value: int) -> str:
    """12001 -> "12 001" (a space between thousands)."""
    return f"{value:,}".replace(",", " ")


def range_label(singular: str, plural: str, first: int, last: int) -> str:
    """A numbered range: "line 4", "lines 120–184"."""
    if first == last:
        return f"{singular} {format_number(first)}"
    return f"{plural} {format_number(first)}–{format_number(last)}"


def quoted(text: str, max_length: int = 60) -> str:
    """A heading or sheet name for a location: 'Install the agent'."""
    return f"'{clip(text, max_length)}'"


def group_by_size(sizes: Sequence[int], target: int) -> Iterator[tuple[int, int]]:
    """Index ranges ``[start, end)`` whose summed sizes stay near ``target``.

    A single item larger than ``target`` forms its own group.
    """
    start = 0
    total = 0
    for index, size in enumerate(sizes):
        if index > start and total + size > target:
            yield start, index
            start = index
            total = 0
        total += size
    if start < len(sizes):
        yield start, len(sizes)


def chunk_sections(sections: Sequence[ExtractedSection]) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    page = 1
    page_used = 0
    for section in sections:
        pieces = split_text(section.text)
        for index, piece in enumerate(pieces):
            location = section.location
            if len(pieces) > 1:
                suffix = f" (part {index + 1})"
                location = (
                    clip(location, KNOWLEDGE_LOCATION_MAX_LENGTH - len(suffix)) + suffix
                )
            if page_used and page_used + len(piece) > KNOWLEDGE_PAGE_CHARS:
                page += 1
                page_used = 0
            page_used += len(piece)
            drafts.append(
                ChunkDraft(
                    ordinal=len(drafts),
                    page=page,
                    location=clip(location, KNOWLEDGE_LOCATION_MAX_LENGTH),
                    heading=clip(section.heading, KNOWLEDGE_HEADING_MAX_LENGTH),
                    text=piece,
                )
            )
    return drafts


def split_text(
    text: str, target: int = CHUNK_TARGET_CHARS, max_chars: int = CHUNK_MAX_CHARS
) -> list[str]:
    """Cut ``text`` into pieces near ``target`` and never above ``max_chars``.

    Cuts prefer a blank line, then a line end, then a space near ``target``.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    pieces: list[str] = []
    position = 0
    while position < len(text):
        if len(text) - position <= max_chars:
            end = len(text)
        else:
            end = _cut(text, position, target, max_chars)
        piece = text[position:end].strip()
        if piece:
            pieces.append(piece)
        position = end
    return pieces


def _cut(text: str, position: int, target: int, max_chars: int) -> int:
    low = position + target // 2
    preferred = position + target
    limit = position + max_chars
    for separator in _SEPARATORS:
        index = text.rfind(separator, low, preferred)
        if index < 0:
            index = text.find(separator, preferred, limit)
        if index >= 0:
            return index + len(separator)
    return limit
