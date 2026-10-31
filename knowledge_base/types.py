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
    # An archive member's folder (its folder tag), "" otherwise.
    folder: str = ""
    # Manual tags, lower-case.
    tags: tuple[str, ...] = ()
    is_folder_instructions: bool = False


@dataclass(frozen=True)
class KnowledgeHit:
    item_id: int
    item_name: str
    location: str
    # The read_knowledge page holding the hit.
    page: int
    # Plain text; matches between « and ».
    snippet: str
    item_folder: str = ""
    item_parent_id: int | None = None


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
    item_folder: str = ""
    tags: tuple[str, ...] = ()
    # The archive an archive member was read from.
    archive_id: int | None = None
    archive_name: str = ""
    # Nearest first; the nearest carries an excerpt on page 1 only.
    folder_instructions: tuple[KnowledgeInstructionRef, ...] = ()


@dataclass(frozen=True)
class KnowledgeExcerpt:
    """A whole chunk retrieved server-side (the Ask AI advisor)."""

    item_id: int
    item_name: str
    location: str
    page: int
    text: str
    item_folder: str = ""


@dataclass(frozen=True)
class ExtractedSection:
    """One piece of extracted text with where it came from."""

    location: str
    heading: str
    text: str


@dataclass(frozen=True)
class ArchiveMemberRef:
    """A member read out of an archive: its normalised path and its bytes.

    ``path`` is uncapped ("/" separators, no "." / ".." / drive / root);
    ``archive_guard.display_label`` caps it for labels. ``is_archive`` marks a
    nested archive the reader expands one level deep.
    """

    path: str
    data: bytes
    is_archive: bool = False


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
    # The archive's name for an archive member (shown on tag-filtered rows).
    archive_name: str = ""


@dataclass(frozen=True)
class KnowledgeItemDetail:
    """The item page (no chunk text; an archive's files come from the browser)."""

    entry: KnowledgeListEntry
    original_name: str
    char_count: int
    chunk_count: int
    # For an archive member: the folder instructions that apply, nearest first.
    folder_instructions: tuple[KnowledgeInstructionRef, ...] = ()


@dataclass(frozen=True)
class KnowledgeFolderRef:
    """What resolving an item's folder instructions needs."""

    item_id: int
    parent_id: int | None
    folder: str


@dataclass(frozen=True)
class KnowledgeInstructionRef:
    """A folder instructions file that applies to an item."""

    item_id: int
    name: str
    folder: str
    kind_label: str
    # pending | extracting | ready | failed
    status: str
    page_count: int
    # Only on the nearest one, and only where an excerpt is shown.
    excerpt: str = ""


@dataclass(frozen=True)
class KnowledgeTagCount:
    tag: str
    count: int


@dataclass(frozen=True)
class KnowledgeTagFacet:
    """The tag filter's choices: manual tags and archive folders, with counts."""

    tags: tuple[KnowledgeTagCount, ...]
    folders: tuple[KnowledgeTagCount, ...]


@dataclass(frozen=True)
class KnowledgeFolderEntry:
    """A folder of an archive (a subfolder row or a breadcrumb)."""

    path: str
    name: str
    # Files in it and its subfolders (0 for breadcrumbs).
    file_count: int


@dataclass(frozen=True)
class KnowledgeArchiveBrowse:
    """One folder of an archive: breadcrumbs, subfolders, files, instructions."""

    archive: KnowledgeItemDetail
    folder: str
    breadcrumbs: tuple[KnowledgeFolderEntry, ...]
    subfolders: tuple[KnowledgeFolderEntry, ...]
    files: tuple[KnowledgeListEntry, ...]
    # Nearest first; the nearest carries an excerpt.
    instructions: tuple[KnowledgeInstructionRef, ...]
