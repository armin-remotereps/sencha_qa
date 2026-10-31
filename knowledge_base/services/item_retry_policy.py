"""When a knowledge item may be re-tried (pure, no ORM queries)."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings

from knowledge_base.models import KnowledgeItemStatus


def is_stuck(
    status: str,
    extract_started_at: datetime | None,
    now: datetime,
    *,
    waiting_since: datetime | None = None,
) -> bool:
    """An extraction that ran past the time limit (its worker died or hung),
    or an item waiting that long for one (its task message was lost)."""
    limit = timedelta(seconds=settings.KNOWLEDGE_BASE_EXTRACT_TIME_LIMIT_SECONDS)
    if status == KnowledgeItemStatus.PENDING:
        return waiting_since is not None and now - waiting_since > limit
    if status != KnowledgeItemStatus.EXTRACTING:
        return False
    if extract_started_at is None:
        return True
    return now - extract_started_at > limit


def can_retry(
    status: str,
    extract_started_at: datetime | None,
    now: datetime,
    *,
    waiting_since: datetime | None = None,
) -> bool:
    """A failed item, or one stuck waiting for or in extraction."""
    return status == KnowledgeItemStatus.FAILED or is_stuck(
        status, extract_started_at, now, waiting_since=waiting_since
    )
