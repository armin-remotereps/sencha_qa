"""Read one page of a ready knowledge base item (two small queries)."""

from __future__ import annotations

from knowledge_base.exceptions import (
    KnowledgeItemNotFoundError,
    KnowledgeItemNotReadyError,
    KnowledgePageOutOfRangeError,
)
from knowledge_base.models import (
    KnowledgeChunk,
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeKind,
)
from knowledge_base.services.list_knowledge_items import ITEM_NOT_FOUND_MESSAGE
from knowledge_base.types import KnowledgePage


def read_knowledge_item(project_id: int, item_id: int, *, page: int) -> KnowledgePage:
    """Page ``page`` (from 1) of the item: its chunks joined with blank lines."""
    item = (
        KnowledgeItem.objects.filter(project_id=project_id, id=item_id)
        .values_list("name", "kind", "status", "page_count")
        .first()
    )
    if item is None:
        raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
    name, kind, status, page_count = item
    if kind == KnowledgeKind.ARCHIVE:
        raise KnowledgeItemNotReadyError(
            f"Item {item_id} is an archive and has no text of its own; list its "
            f"files with list_knowledge(archive_id={item_id}) and read those."
        )
    if status != KnowledgeItemStatus.READY:
        label = (
            KnowledgeItemStatus(status).label
            if status in KnowledgeItemStatus.values
            else status
        )
        raise KnowledgeItemNotReadyError(
            f"Item {item_id} has no readable text yet (status: {label})."
        )
    page_count = max(page_count, 1)
    if page < 1 or page > page_count:
        raise KnowledgePageOutOfRangeError(
            f"Page {page} does not exist: item {item_id} has {page_count} "
            f"page{'s' if page_count != 1 else ''}."
        )
    chunks = list(
        KnowledgeChunk.objects.filter(item_id=item_id, page=page)
        .order_by("ordinal")
        .values_list("location", "text")
    )
    locations = [location for location, _ in chunks if location]
    if not locations:
        location = ""
    elif locations[0] == locations[-1]:
        location = locations[0]
    else:
        location = f"{locations[0]} – {locations[-1]}"
    return KnowledgePage(
        item_id=item_id,
        item_name=name,
        kind=kind,
        page=page,
        page_count=page_count,
        location=location,
        text="\n\n".join(text for _, text in chunks),
        has_next=page < page_count,
    )
