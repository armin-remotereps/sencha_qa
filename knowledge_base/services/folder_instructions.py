"""Resolve which folder instructions apply to archive members (read time).

An instruction file applies to its folder and every subfolder of the same
archive. Resolving costs one query whatever the number of items (served by
the partial index on (parent, folder)); an excerpt costs one more.
"""

from __future__ import annotations

from collections.abc import Sequence

from knowledge_base.folder_tree import ancestor_folders
from knowledge_base.models import (
    KnowledgeChunk,
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeKind,
)
from knowledge_base.types import KnowledgeFolderRef, KnowledgeInstructionRef

FOLDER_INSTRUCTIONS_PER_ITEM = 3
FOLDER_INSTRUCTION_EXCERPT_CHARS = 600
_ELLIPSIS = "…"


def instructions_for_items(
    project_id: int, refs: Sequence[KnowledgeFolderRef]
) -> dict[int, tuple[KnowledgeInstructionRef, ...]]:
    """Instructions per item id, nearest first, at most 3, never the item itself.

    One query whatever the number of refs; items without a parent get none
    (and are left out of the result).
    """
    members = [ref for ref in refs if ref.parent_id is not None]
    if not members:
        return {}
    parent_ids = {ref.parent_id for ref in members}
    folders = {folder for ref in members for folder in ancestor_folders(ref.folder)}
    rows = (
        KnowledgeItem.objects.filter(
            project_id=project_id,
            parent_id__in=parent_ids,
            is_folder_instructions=True,
            folder__in=folders,
        )
        .order_by("id")
        .values_list(
            "id", "parent_id", "folder", "name", "kind", "status", "page_count"
        )
    )
    by_folder: dict[tuple[int, str], list[KnowledgeInstructionRef]] = {}
    for item_id, parent_id, folder, name, kind, status, page_count in rows:
        by_folder.setdefault((parent_id, folder), []).append(
            KnowledgeInstructionRef(
                item_id=item_id,
                name=name,
                folder=folder,
                kind_label=_kind_label(kind),
                status=status,
                page_count=page_count,
            )
        )
    resolved: dict[int, tuple[KnowledgeInstructionRef, ...]] = {}
    for ref in members:
        assert ref.parent_id is not None
        found: list[KnowledgeInstructionRef] = []
        for folder in ancestor_folders(ref.folder):
            for instruction in by_folder.get((ref.parent_id, folder), ()):
                if instruction.item_id != ref.item_id:
                    found.append(instruction)
            if len(found) >= FOLDER_INSTRUCTIONS_PER_ITEM:
                break
        if found:
            resolved[ref.item_id] = tuple(found[:FOLDER_INSTRUCTIONS_PER_ITEM])
    return resolved


def first_ready(
    instructions: Sequence[KnowledgeInstructionRef],
) -> KnowledgeInstructionRef | None:
    """The nearest instruction that is ready (the one an excerpt is shown for)."""
    return next(
        (ref for ref in instructions if ref.status == KnowledgeItemStatus.READY),
        None,
    )


def instruction_excerpt(project_id: int, item_id: int) -> str:
    """The start of a ready instruction item's first chunk (≤ 600 chars), else ""."""
    text = (
        KnowledgeChunk.objects.filter(
            project_id=project_id,
            item_id=item_id,
            ordinal=0,
            item__status=KnowledgeItemStatus.READY,
        )
        .values_list("text", flat=True)
        .first()
    )
    if not text:
        return ""
    if len(text) <= FOLDER_INSTRUCTION_EXCERPT_CHARS:
        return text
    cut = FOLDER_INSTRUCTION_EXCERPT_CHARS - len(_ELLIPSIS)
    return text[:cut].rstrip() + _ELLIPSIS


def _kind_label(kind: str) -> str:
    # list_knowledge_items.kind_label, not imported: that module imports this one.
    try:
        return str(KnowledgeKind(kind).label)
    except ValueError:
        return kind
