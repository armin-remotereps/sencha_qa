"""Manual tags and archive folder paths (pure, no ORM).

Manual tags are stored lower-case in ``KnowledgeItem.tags``. A member's
folder path is its folder tag: it lives in ``KnowledgeItem.folder`` only,
keeps its case and is matched case-insensitively
(``knowledge_base.services.tag_filter``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from knowledge_base.models import (
    KNOWLEDGE_FOLDER_MAX_LENGTH,
    KNOWLEDGE_MAX_TAGS_PER_ITEM,
    KNOWLEDGE_TAG_MAX_LENGTH,
)

_WHITESPACE = re.compile(r"\s+")
_TAG_SEPARATORS = re.compile(r"[,\n]")
# Postgres's default parser reads "docs/install/windows" as one token, so a
# folder is split into words before it goes into a search vector.
_PATH_WORD_SEPARATORS = re.compile(r"[/\\_.\-]+")


def normalise_tag(raw: str) -> str:
    """Printable, collapsed whitespace, no commas, lower-case, capped; "" if empty."""
    text = "".join(char for char in raw if char.isprintable()).replace(",", " ")
    text = _WHITESPACE.sub(" ", text).strip().casefold()
    return text[:KNOWLEDGE_TAG_MAX_LENGTH].strip()


def parse_tag_input(text: str) -> tuple[str, ...]:
    """Tags typed by a member (comma or newline separated), de-duplicated in order.

    Not capped: the form reports more than ``KNOWLEDGE_MAX_TAGS_PER_ITEM``.
    """
    return _unique(normalise_tag(part) for part in _TAG_SEPARATORS.split(text))


def merge_tags(*groups: Iterable[str]) -> tuple[str, ...]:
    """Normalised tags of every group, de-duplicated in order and capped."""
    merged = _unique(normalise_tag(tag) for group in groups for tag in group)
    return merged[:KNOWLEDGE_MAX_TAGS_PER_ITEM]


def normalise_folder_query(raw: str) -> str:
    """A tag query read as a folder path: no surrounding slashes, case kept."""
    return raw.strip().replace("\\", "/").strip("/").strip()


def split_member_path(path: str) -> tuple[str, str]:
    """``(folder, file_name)`` of a member path; the folder capped by segments.

    A folder over ``KNOWLEDGE_FOLDER_MAX_LENGTH`` loses whole trailing
    segments, so it stays a real ancestor folder of the file.
    """
    folder, _, file_name = path.rpartition("/")
    if len(folder) > KNOWLEDGE_FOLDER_MAX_LENGTH:
        kept: list[str] = []
        length = 0
        for segment in folder.split("/"):
            extra = len(segment) + (1 if kept else 0)
            if length + extra > KNOWLEDGE_FOLDER_MAX_LENGTH:
                break
            kept.append(segment)
            length += extra
        folder = "/".join(kept)
    return folder, file_name


def item_path(folder: str, name: str) -> str:
    """``folder/name``, or ``name`` at the root."""
    return f"{folder}/{name}" if folder else name


def search_label_text(folder: str, tags: Sequence[str]) -> str:
    """Folder words plus the manual tags, for the search vector's B weight."""
    words = " ".join(part for part in _PATH_WORD_SEPARATORS.split(folder) if part)
    return " ".join(part for part in (words, *tags) if part)


def _unique(tags: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for tag in tags:
        if tag:
            seen.setdefault(tag, None)
    return tuple(seen)
