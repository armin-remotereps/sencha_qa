"""When Force stop is allowed for a setup turn (leaf: models and settings only)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from django.conf import settings

from machine_setup.models import SetupTurn, SetupTurnStatus


def force_stop_in(turn: SetupTurn, now: datetime) -> int | None:
    """Seconds until Force stop is allowed (0 = now).

    ``None`` unless the turn is running and Stop was pressed. A turn flagged
    without ``stop_requested_at`` (before that field existed) counts as long ago.
    """
    if turn.status != SetupTurnStatus.RUNNING or not turn.stop_requested:
        return None
    if turn.stop_requested_at is None:
        return 0
    grace: int = settings.MACHINE_SETUP_FORCE_STOP_GRACE_SECONDS
    allowed_at = turn.stop_requested_at + timedelta(seconds=grace)
    left = (allowed_at - now).total_seconds()
    return max(0, math.ceil(left))
