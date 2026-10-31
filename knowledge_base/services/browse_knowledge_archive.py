"""One folder of an archive item: breadcrumbs, subfolders, files, instructions."""

from __future__ import annotations

from knowledge_base.types import KnowledgeArchiveBrowse


def archive_folder_counts(project_id: int, archive_id: int) -> dict[str, int]:
    """Files directly in each stored folder of the archive (one query)."""
    raise NotImplementedError


def browse_knowledge_archive(
    project_id: int, archive_id: int, *, folder: str
) -> KnowledgeArchiveBrowse:
    """Raise KnowledgeItemNotFoundError / KnowledgeFolderNotFoundError."""
    raise NotImplementedError
