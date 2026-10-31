"""Shared steps of the write services: task names, enqueueing and broadcasts.

Services send tasks by name (``knowledge_base.tasks`` imports the services, so
the services cannot import it back).
"""

from __future__ import annotations

from django.db import transaction

from auto_tester.celery import app as celery_app
from knowledge_base.events import KnowledgeItemEventPayload, broadcast_item
from knowledge_base.models import KnowledgeItem, KnowledgeItemStatus, KnowledgeKind

EXTRACT_KNOWLEDGE_ITEM_TASK = "knowledge_base.tasks.extract_knowledge_item"
EXPAND_KNOWLEDGE_ARCHIVE_TASK = "knowledge_base.tasks.expand_knowledge_archive"
COPY_PROJECT_KNOWLEDGE_TASK = "knowledge_base.tasks.copy_project_knowledge"
SWEEP_KNOWLEDGE_STORAGE_TASK = "knowledge_base.tasks.sweep_knowledge_storage"

ITEM_NOT_FOUND_MESSAGE = "This knowledge base item no longer exists."


def enqueue_item_processing(item_id: int, kind: str) -> None:
    """Queue the item's extraction (or expansion) once the transaction commits."""
    task_name = (
        EXPAND_KNOWLEDGE_ARCHIVE_TASK
        if kind == KnowledgeKind.ARCHIVE
        else EXTRACT_KNOWLEDGE_ITEM_TASK
    )
    transaction.on_commit(lambda: celery_app.send_task(task_name, args=[item_id]))


def item_event_payload(item: KnowledgeItem) -> KnowledgeItemEventPayload:
    return KnowledgeItemEventPayload(
        id=item.pk,
        parent_id=item.parent_id,
        status=str(item.status),
        status_label=KnowledgeItemStatus(item.status).label,
        warning=item.warning,
        error=item.error,
        page_count=item.page_count,
    )


def broadcast_item_state(item: KnowledgeItem) -> None:
    """Send the item's current (in-memory) state to the tab after commit."""
    broadcast_item(item.project_id, item_event_payload(item))
