"""One folder of an archive item: breadcrumbs, subfolders, files, instructions.

At most five queries: the archive, its folder counts, the files directly in
the folder, the folder instructions and one excerpt of the nearest ready one.
"""

from __future__ import annotations

import dataclasses

from django.db.models import Count

from knowledge_base.exceptions import (
    KnowledgeFolderNotFoundError,
    KnowledgeItemNotFoundError,
)
from knowledge_base.folder_tree import breadcrumbs, folder_exists, subfolders
from knowledge_base.models import (
    KNOWLEDGE_FOLDER_MAX_LENGTH,
    KnowledgeItem,
    KnowledgeKind,
)
from knowledge_base.services.folder_instructions import (
    first_ready,
    instruction_excerpt,
    instructions_for_items,
)
from knowledge_base.services.list_knowledge_items import (
    ITEM_NOT_FOUND_MESSAGE,
    entries_of,
    get_knowledge_item_detail,
    project_items,
)
from knowledge_base.types import (
    KnowledgeArchiveBrowse,
    KnowledgeFolderRef,
    KnowledgeInstructionRef,
)

FOLDER_NOT_FOUND_MESSAGE = "This folder does not exist in the archive."
# No item has id 0, so the folder's own instruction file is never excluded.
_FOLDER_REF_ID = 0


def archive_folder_counts(project_id: int, archive_id: int) -> dict[str, int]:
    """Files directly in each stored folder of the archive (one query)."""
    rows = (
        KnowledgeItem.objects.filter(project_id=project_id, parent_id=archive_id)
        .values("folder")
        .annotate(count=Count("id"))
        .order_by()
        .values_list("folder", "count")
    )
    return {folder: count for folder, count in rows}


def browse_knowledge_archive(
    project_id: int, archive_id: int, *, folder: str
) -> KnowledgeArchiveBrowse:
    """Raise KnowledgeItemNotFoundError / KnowledgeFolderNotFoundError."""
    if len(folder) > KNOWLEDGE_FOLDER_MAX_LENGTH:
        raise KnowledgeFolderNotFoundError(FOLDER_NOT_FOUND_MESSAGE)
    archive = get_knowledge_item_detail(project_id, archive_id)
    summary = archive.entry.summary
    if summary.kind != KnowledgeKind.ARCHIVE or summary.parent_id is not None:
        raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
    counts = archive_folder_counts(project_id, archive_id)
    if not folder_exists(counts, folder):
        raise KnowledgeFolderNotFoundError(FOLDER_NOT_FOUND_MESSAGE)
    files = (
        entries_of(
            project_items(project_id)
            .filter(parent_id=archive_id, folder=folder)
            .order_by("name", "id")
        )
        if counts.get(folder)
        else []
    )
    return KnowledgeArchiveBrowse(
        archive=archive,
        folder=folder,
        breadcrumbs=breadcrumbs(folder),
        subfolders=subfolders(counts, folder),
        files=tuple(files),
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
