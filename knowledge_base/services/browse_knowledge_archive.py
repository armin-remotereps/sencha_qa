"""One folder of an archive item: breadcrumbs, subfolders, files, instructions.

At most five queries: the archive (none when the caller already loaded it),
its folder counts, the files directly in the folder (at most ``file_limit``),
the folder instructions and one excerpt of the nearest ready one.
"""

from __future__ import annotations

import dataclasses

from knowledge_base.exceptions import (
    KnowledgeFolderNotFoundError,
    KnowledgeItemNotFoundError,
)
from knowledge_base.folder_tree import breadcrumbs, folder_exists, subfolders
from knowledge_base.models import KNOWLEDGE_FOLDER_MAX_LENGTH, KnowledgeKind
from knowledge_base.services.folder_instructions import (
    first_ready,
    instruction_excerpt,
    instructions_for_items,
)
from knowledge_base.services.list_knowledge_items import (
    ITEM_NOT_FOUND_MESSAGE,
    archive_folder_counts,
    entries_of,
    get_knowledge_item_detail,
    project_items,
)
from knowledge_base.types import (
    KnowledgeArchiveBrowse,
    KnowledgeFolderRef,
    KnowledgeInstructionRef,
    KnowledgeItemDetail,
)

FOLDER_NOT_FOUND_MESSAGE = "This folder does not exist in the archive."
# No item has id 0, so the folder's own instruction file is never excluded.
_FOLDER_REF_ID = 0


def browse_knowledge_archive(
    project_id: int,
    archive_id: int,
    *,
    folder: str,
    file_limit: int | None = None,
    archive: KnowledgeItemDetail | None = None,
) -> KnowledgeArchiveBrowse:
    """Raise KnowledgeItemNotFoundError / KnowledgeFolderNotFoundError.

    ``archive`` is the item's detail when the caller already loaded it (it must
    be ``archive_id`` of ``project_id``); ``file_limit`` caps the files shown,
    ``files_capped`` telling whether more exist.
    """
    if len(folder) > KNOWLEDGE_FOLDER_MAX_LENGTH:
        raise KnowledgeFolderNotFoundError(FOLDER_NOT_FOUND_MESSAGE)
    if archive is None:
        archive = get_knowledge_item_detail(project_id, archive_id)
    summary = archive.entry.summary
    if (
        summary.id != archive_id
        or summary.kind != KnowledgeKind.ARCHIVE
        or summary.parent_id is not None
    ):
        raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
    counts = archive_folder_counts(project_id, archive_id)
    if not folder_exists(counts, folder):
        raise KnowledgeFolderNotFoundError(FOLDER_NOT_FOUND_MESSAGE)
    files = (
        entries_of(
            project_items(project_id)
            .filter(parent_id=archive_id, folder=folder)
            .order_by("name", "id"),
            limit=None if file_limit is None else file_limit + 1,
        )
        if counts.get(folder)
        else []
    )
    files_capped = file_limit is not None and len(files) > file_limit
    return KnowledgeArchiveBrowse(
        archive=archive,
        folder=folder,
        breadcrumbs=breadcrumbs(folder),
        subfolders=subfolders(counts, folder),
        files=tuple(files[:file_limit] if files_capped else files),
        files_capped=files_capped,
        instructions=_folder_instructions(project_id, archive_id, folder),
    )


def _folder_instructions(
    project_id: int, archive_id: int, folder: str
) -> tuple[KnowledgeInstructionRef, ...]:
    """The instructions of ``folder`` (its own file included), nearest first;
    the nearest ready one carries an excerpt."""
    instructions = instructions_for_items(
        project_id,
        [
            KnowledgeFolderRef(
                item_id=_FOLDER_REF_ID, parent_id=archive_id, folder=folder
            )
        ],
    ).get(_FOLDER_REF_ID, ())
    nearest = first_ready(instructions)
    if nearest is None:
        return instructions
    excerpt = instruction_excerpt(project_id, nearest.item_id)
    return tuple(
        dataclasses.replace(ref, excerpt=excerpt) if ref is nearest else ref
        for ref in instructions
    )
