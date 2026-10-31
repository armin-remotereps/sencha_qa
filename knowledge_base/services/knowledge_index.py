"""The short index of a project's knowledge base for agent prompts (one query)."""

from __future__ import annotations

from collections.abc import Sequence

from django.db.models import Count, Q, Window

from knowledge_base.models import KnowledgeItem, KnowledgeItemStatus, KnowledgeKind
from knowledge_base.services.list_knowledge_items import kind_label
from knowledge_base.tool_text import fenced, one_line, quoted_name

INDEX_MAX_ITEMS = 40
INDEX_MAX_CHARS = 3000
_DESCRIPTION_CHARS = 200
_HEADER = (
    "Knowledge base items (id, name, kind, size, tags, description), newest first:"
)
_FOOTER = (
    "Search with search_knowledge(query); read with read_knowledge(item_id, page)."
)


def knowledge_index_text(project_id: int) -> str | None:
    """The fenced index, or None exactly when there is no ready top-level item."""
    ready = KnowledgeItemStatus.READY
    rows = list(
        KnowledgeItem.objects.filter(
            project_id=project_id, parent__isnull=True, status=ready
        )
        .annotate(
            member_count=Count("members", filter=Q(members__status=ready)),
            total=Window(expression=Count("id")),
        )
        .order_by("-created_at", "-id")
        .values_list(
            "id",
            "name",
            "kind",
            "page_count",
            "description",
            "tags",
            "member_count",
            "total",
        )[:INDEX_MAX_ITEMS]
    )
    if not rows:
        return None
    total = int(rows[0][7])
    # Room for the fence, the note, the header and a "N more" line.
    budget = INDEX_MAX_CHARS - len(fenced("")) - len(_HEADER) - len(_FOOTER) - 80
    lines: list[str] = []
    for item_id, name, kind, page_count, description, tags, member_count, _ in rows:
        line = _index_line(
            item_id, name, kind, page_count, description, tags, member_count
        )
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
    more = total - len(lines)
    if more > 0:
        lines.append(f"{more} more: call list_knowledge")
    return fenced("\n".join([_HEADER, *lines])) + "\n" + _FOOTER


def _index_line(
    item_id: int,
    name: str,
    kind: str,
    page_count: int,
    description: str,
    tags: Sequence[str],
    member_count: int,
) -> str:
    if member_count or kind == KnowledgeKind.ARCHIVE:
        size = f"{member_count} file{'' if member_count == 1 else 's'}"
    else:
        pages = max(page_count, 1)
        size = f"{pages} page{'' if pages == 1 else 's'}"
    line = f"- id={item_id} {quoted_name(name)} ({one_line(kind_label(kind))}, {size})"
    manual = ", ".join(filter(None, (one_line(tag) for tag in tags)))
    if manual:
        line += f" tags: {manual}"
    summary = one_line(description)
    if summary:
        if len(summary) > _DESCRIPTION_CHARS:
            summary = summary[: _DESCRIPTION_CHARS - 1].rstrip() + "…"
        line += f": {summary}"
    return line
