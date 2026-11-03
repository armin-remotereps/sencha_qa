"""The time budget of an analysis tool call (pure).

A tool's AI calls must end before the turn's deadline (a ``time.monotonic()``
value); every call is assumed to take up to ``RUN_TRIAGE_CALL_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import time
from typing import Literal

from django.conf import settings

# Why a tool stopped before its work was done ("" = it did not).
StopReason = Literal["", "stop", "deadline"]

DEADLINE_STOP_TEXT = (
    "Stopped before the turn's time limit; {remaining} remain: continue in a "
    "new message."
)


def fits_before(deadline: float | None, *, waves: int) -> bool:
    """Whether ``waves`` consecutive call waves can finish before ``deadline``."""
    if deadline is None:
        return True
    needed = waves * float(settings.RUN_TRIAGE_CALL_TIMEOUT_SECONDS)
    return time.monotonic() + needed <= deadline
