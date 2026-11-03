"""When a Machine setup turn counts as live for the case analysis (a leaf).

Queued or running, and not older than ``SETUP_TURN_STALE_AFTER`` (counted
from its start, or from its creation while still queued): the same rule as
``machine_setup.turn_state.live_turn_exists``. Used by the analysis card,
report and the Machine setup page's Analysis section.
"""

from __future__ import annotations

from datetime import datetime

from django.db.models import Q

from machine_setup.models import LIVE_TURN_STATUSES
from machine_setup.turn_state import SETUP_TURN_STALE_AFTER


def live_turn_q(now: datetime, *, prefix: str = "") -> Q:
    """A filter on ``SetupTurn`` rows (or, with ``prefix`` such as
    ``"turns__"``, on a relation to them) that are live at ``now``."""
    cutoff = now - SETUP_TURN_STALE_AFTER
    return Q(**{f"{prefix}status__in": LIVE_TURN_STATUSES}) & (
        Q(**{f"{prefix}started_at__gte": cutoff})
        | Q(**{f"{prefix}started_at__isnull": True, f"{prefix}created_at__gte": cutoff})
    )


def is_turn_live(
    status: str, started_at: datetime | None, created_at: datetime, now: datetime
) -> bool:
    """``live_turn_q`` for values already loaded (no query)."""
    if status not in LIVE_TURN_STATUSES:
        return False
    cutoff = now - SETUP_TURN_STALE_AFTER
    if started_at is not None:
        return started_at >= cutoff
    return created_at >= cutoff
