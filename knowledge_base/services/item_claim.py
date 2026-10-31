"""Claiming an item for extraction and failing a claimed item.

A claim is ``status=extracting`` plus ``extract_started_at``; that timestamp is
the claim token. A worker writes its result only while its token still holds,
so a retried or re-claimed item never gets a stale worker's result.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from knowledge_base.models import (
    KNOWLEDGE_ERROR_MAX_LENGTH,
    KnowledgeChunk,
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeKind,
)
from knowledge_base.services.item_processing import broadcast_item_state

TIMED_OUT_MESSAGE = "This file took too long to read."


def claim_item(item_id: int, *, archive: bool) -> KnowledgeItem | None:
    """Mark a pending (or stuck) item as extracting; None when not claimable.

    ``archive`` selects archive items (expansion) or every other kind.
    """
    now = timezone.now()
    stale_before = now - timedelta(
        seconds=settings.KNOWLEDGE_BASE_EXTRACT_TIME_LIMIT_SECONDS
    )
    claimable = (
        Q(status=KnowledgeItemStatus.PENDING)
        | Q(status=KnowledgeItemStatus.EXTRACTING, extract_started_at__lt=stale_before)
        | Q(status=KnowledgeItemStatus.EXTRACTING, extract_started_at__isnull=True)
    )
    kind_filter = (
        Q(kind=KnowledgeKind.ARCHIVE) if archive else ~Q(kind=KnowledgeKind.ARCHIVE)
    )
    claimed = KnowledgeItem.objects.filter(claimable, kind_filter, pk=item_id).update(
        status=KnowledgeItemStatus.EXTRACTING,
        extract_started_at=now,
        error="",
        warning="",
        updated_at=now,
    )
    if not claimed:
        return None
    item = (
        KnowledgeItem.objects.select_related("blob")
        .filter(pk=item_id, extract_started_at=now)
        .first()
    )
    if item is not None:
        broadcast_item_state(item)
    return item


def lock_claimed_item(item_id: int, claimed_at: datetime) -> KnowledgeItem | None:
    """Lock the item row if this worker's claim still holds (in a transaction)."""
    return (
        KnowledgeItem.objects.select_for_update()
        .filter(
            pk=item_id,
            status=KnowledgeItemStatus.EXTRACTING,
            extract_started_at=claimed_at,
        )
        .first()
    )


def fail_claimed_item(item_id: int, claimed_at: datetime, message: str) -> None:
    """Fail the item (and drop its chunks) if this worker's claim still holds."""
    with transaction.atomic():
        item = lock_claimed_item(item_id, claimed_at)
        if item is None:
            return
        _fail_locked(item, message)


def _fail_locked(item: KnowledgeItem, message: str) -> None:
    KnowledgeChunk.objects.filter(item_id=item.pk).delete()
    item.status = KnowledgeItemStatus.FAILED
    item.error = message[:KNOWLEDGE_ERROR_MAX_LENGTH]
    item.warning = ""
    item.char_count = 0
    item.page_count = 0
    item.chunk_count = 0
    item.save(
        update_fields=[
            "status",
            "error",
            "warning",
            "char_count",
            "page_count",
            "chunk_count",
            "updated_at",
        ]
    )
    broadcast_item_state(item)
