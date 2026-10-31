"""The note a setup chat turn gets for each referenced knowledge base item."""

from __future__ import annotations

from knowledge_base.models import KnowledgeItemStatus
from knowledge_base.tags import item_path
from knowledge_base.tool_text import quoted_name
from knowledge_base.types import KnowledgeItemSummary

# Composer picker cap per USER message.
MAX_KNOWLEDGE_REFS_PER_MESSAGE = 5


def knowledge_reference_note(summary: KnowledgeItemSummary) -> str:
    """One line telling the agent which item the person pointed at and how to read it."""
    # Quoted, one line, neutralised: the path is member-controlled text.
    name = quoted_name(item_path(summary.folder, summary.name))
    if summary.status != KnowledgeItemStatus.READY:
        return (
            f"[Referenced knowledge base item id={summary.id} {name} "
            f"({summary.kind_label}); not readable yet: its status is "
            f"{summary.status}.]"
        )
    if summary.member_count:
        return (
            f"[Referenced knowledge base archive id={summary.id} {name} with "
            f"{summary.member_count} member files. List them with "
            f"list_knowledge(archive_id={summary.id}).]"
        )
    pages = max(summary.page_count, 1)
    return (
        f"[Referenced knowledge base item id={summary.id} {name} "
        f"({summary.kind_label}), {pages} page{'s' if pages != 1 else ''}. Read it "
        f"with read_knowledge(item_id={summary.id}, page=N) or search it with "
        f"search_knowledge(query, item_id={summary.id}).]"
    )
