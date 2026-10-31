"""Contract dataclasses of the knowledge base (shared with other apps)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class KnowledgeItemSummary:
    id: int
    name: str
    kind: str
    kind_label: str
    description: str
    # pending | extracting | ready | failed
    status: str
    size_bytes: int
    page_count: int
    parent_id: int | None
    member_count: int


@dataclass(frozen=True)
class KnowledgeHit:
    item_id: int
    item_name: str
    location: str
    # The read_knowledge page holding the hit.
    page: int
    # Plain text; matches between « and ».
    snippet: str


@dataclass(frozen=True)
class KnowledgePage:
    item_id: int
    item_name: str
    kind: str
    page: int
    page_count: int
    location: str
    text: str
    has_next: bool


@dataclass(frozen=True)
class KnowledgeExcerpt:
    """A whole chunk retrieved server-side (the Ask AI advisor)."""

    item_id: int
    item_name: str
    location: str
    page: int
    text: str


@dataclass(frozen=True)
class ExtractedSection:
    """One piece of extracted text with where it came from."""

    location: str
    heading: str
    text: str


@dataclass(frozen=True)
class ArchiveMemberRef:
    """A member read out of an archive: its display label and its bytes."""

    label: str
    data: bytes


@dataclass(frozen=True)
class RefusedFile:
    name: str
    reason: str


@dataclass(frozen=True)
class UploadOutcome:
    created_ids: tuple[int, ...]
    refused: tuple[RefusedFile, ...]


@dataclass(frozen=True)
class KnowledgeListEntry:
    """One row of the Knowledge base tab: the summary plus page-only fields."""

    summary: KnowledgeItemSummary
    error: str
    warning: str
    # Display name of the uploader, "" when unknown or deleted.
    uploaded_by: str
    created_at: datetime
    # True while extraction may be re-tried (failed, or extracting and stuck).
    can_retry: bool


@dataclass(frozen=True)
class KnowledgeItemDetail:
    """The item page: the item and its archive members (no chunk text)."""

    entry: KnowledgeListEntry
    original_name: str
    char_count: int
    chunk_count: int
    members: tuple[KnowledgeListEntry, ...]
