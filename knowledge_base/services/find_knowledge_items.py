"""Look up items for rendering references; absent ids are simply left out."""

from __future__ import annotations

from collections.abc import Sequence

from knowledge_base.services.list_knowledge_items import project_items, summaries_of
from knowledge_base.types import KnowledgeItemSummary


def find_knowledge_items(
    project_id: int, ids: Sequence[int]
) -> dict[int, KnowledgeItemSummary]:
    """Summaries by id; missing, deleted or other-project ids are absent. One query."""
    wanted = set(ids)
    if not wanted:
        return {}
    return {
        summary.id: summary
        for summary in summaries_of(project_items(project_id).filter(id__in=wanted))
    }
