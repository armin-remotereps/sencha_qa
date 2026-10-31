"""Resolve which folder instructions apply to archive members (read time)."""

from __future__ import annotations

from collections.abc import Sequence

from knowledge_base.types import KnowledgeFolderRef, KnowledgeInstructionRef

FOLDER_INSTRUCTIONS_PER_ITEM = 3
FOLDER_INSTRUCTION_EXCERPT_CHARS = 600


def instructions_for_items(
    project_id: int, refs: Sequence[KnowledgeFolderRef]
) -> dict[int, tuple[KnowledgeInstructionRef, ...]]:
    """Instructions per item id, nearest first, at most 3, never the item itself.

    One query whatever the number of refs; items without a parent get none.
    """
    raise NotImplementedError


def instruction_excerpt(project_id: int, item_id: int) -> str:
    """The start of a ready instruction item's first chunk (≤ 600 chars), else ""."""
    raise NotImplementedError
