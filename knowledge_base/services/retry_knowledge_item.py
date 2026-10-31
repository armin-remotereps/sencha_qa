"""Retry the extraction of a failed or stuck knowledge item."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from knowledge_base.exceptions import KnowledgeBaseError, KnowledgeItemNotFoundError
from knowledge_base.models import KnowledgeItem, KnowledgeItemStatus
from knowledge_base.services.item_processing import (
    ITEM_NOT_FOUND_MESSAGE,
    broadcast_item_state,
    enqueue_item_processing,
)
from knowledge_base.services.item_retry_policy import can_retry

NOT_RETRYABLE_MESSAGE = "Only a failed or stuck item can be retried."


def retry_knowledge_item(*, project_id: int, item_id: int) -> None:
    """Put the item back to pending and queue its extraction after commit."""
    with transaction.atomic():
        item = (
            KnowledgeItem.objects.select_for_update()
            .filter(pk=item_id, project_id=project_id)
            .first()
        )
        if item is None:
            raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
        if not can_retry(
            item.status,
            item.extract_started_at,
            timezone.now(),
            waiting_since=item.updated_at,
        ):
            raise KnowledgeBaseError(NOT_RETRYABLE_MESSAGE)
        item.status = KnowledgeItemStatus.PENDING
        item.error = ""
        item.warning = ""
        item.extract_started_at = None
        item.save(
            update_fields=[
                "status",
                "error",
                "warning",
                "extract_started_at",
                "updated_at",
            ]
        )
        broadcast_item_state(item)
        enqueue_item_processing(item.pk, item.kind)
