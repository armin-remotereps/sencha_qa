"""Validate item ids a member or an agent referenced (composer, profile writes)."""

from __future__ import annotations

from collections.abc import Sequence

from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.services.list_knowledge_items import project_items, summaries_of
from knowledge_base.types import KnowledgeItemSummary


def resolve_knowledge_refs(
    project_id: int, item_ids: Sequence[int]
) -> list[KnowledgeItemSummary]:
    """Summaries of ``item_ids`` in input order, without duplicates; one query.

    Raises ``KnowledgeItemNotFoundError`` when any id is not an item of the
    project (any status; archive members included).
    """
    wanted = list(dict.fromkeys(item_ids))
    if not wanted:
        return []
    found = {
        summary.id: summary
        for summary in summaries_of(project_items(project_id).filter(id__in=wanted))
    }
    missing = [item_id for item_id in wanted if item_id not in found]
    if missing:
        raise KnowledgeItemNotFoundError(
            f"Knowledge base item {missing[0]} does not exist in this project "
            "(it may have been deleted)."
        )
    return [found[item_id] for item_id in wanted]
