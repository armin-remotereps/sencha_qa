"""Close sessions left open longer than ``TEST_SESSION_MAX_HOURS`` (beat)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F

from test_sessions.models import TestSession
from test_sessions.services.record_case_activity import (
    freeze_finished_cases_of_sessions,
)

logger = logging.getLogger(__name__)

# Sessions closed per transaction; the loop runs until none is left.
CLOSE_BATCH_SIZE = 100


def _close_batch(cutoff: datetime, max_open: timedelta, now: datetime) -> int:
    """Close up to ``CLOSE_BATCH_SIZE`` stale sessions and freeze their
    finished cases (3 queries)."""
    with transaction.atomic():
        session_ids = list(
            TestSession.objects.select_for_update(skip_locked=True)
            .filter(ended_at__isnull=True, started_at__lt=cutoff)
            .order_by("id")
            .values_list("id", flat=True)[:CLOSE_BATCH_SIZE]
        )
        if not session_ids:
            return 0
        TestSession.objects.filter(id__in=session_ids, ended_at__isnull=True).update(
            ended_at=F("started_at") + max_open,
            closed_automatically=True,
            updated_at=now,
        )
        freeze_finished_cases_of_sessions(session_ids, now)
    return len(session_ids)


def close_stale_sessions(now: datetime) -> int:
    """End every session open for more than ``TEST_SESSION_MAX_HOURS`` at
    ``started_at + max``, mark it ``closed_automatically`` and freeze its
    finished cases, as Stop does. Returns how many were closed; 0 (and no
    query) when ``TEST_SESSION_MAX_HOURS`` is 0 (auto-close off)."""
    max_hours: int = settings.TEST_SESSION_MAX_HOURS
    if max_hours <= 0:
        return 0
    max_open = timedelta(hours=max_hours)
    cutoff = now - max_open
    closed = 0
    while True:
        batch = _close_batch(cutoff, max_open, now)
        closed += batch
        if batch < CLOSE_BATCH_SIZE:
            break
    if closed:
        logger.info("Closed %d stale test session(s)", closed)
    return closed
