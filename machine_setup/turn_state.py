"""Time limits of a setup turn and the "is a turn live" test built on them.

A leaf module (imports only ``machine_setup.models``) so ``projects`` can gate
run start and resume on it without importing the agent runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q

from machine_setup.models import LIVE_TURN_STATUSES, SetupTurn

# Celery limits of `run_setup_turn`: the agent deadline plus headroom for an
# in-flight tool. A live turn older than the hard limit plus a minute belongs
# to a killed worker.
SETUP_TURN_SOFT_TIME_LIMIT: int = settings.MACHINE_SETUP_TURN_TIMEOUT_SECONDS + 300
SETUP_TURN_TIME_LIMIT: int = SETUP_TURN_SOFT_TIME_LIMIT + 60
SETUP_TURN_STALE_AFTER = timedelta(seconds=SETUP_TURN_TIME_LIMIT + 60)


def is_turn_stale(turn: SetupTurn, now: datetime) -> bool:
    """Whether a queued or running turn has outlived any worker that could run it."""
    if turn.status not in LIVE_TURN_STATUSES:
        return False
    since = turn.started_at or turn.created_at
    return since < now - SETUP_TURN_STALE_AFTER


def live_turn_exists(project_id: int, now: datetime) -> bool:
    """Whether a queued or running, non-stale turn exists for the project."""
    cutoff = now - SETUP_TURN_STALE_AFTER
    return SetupTurn.objects.filter(
        Q(started_at__gte=cutoff) | Q(started_at__isnull=True, created_at__gte=cutoff),
        project_id=project_id,
        status__in=LIVE_TURN_STATUSES,
    ).exists()
