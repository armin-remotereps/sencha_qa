"""Lenient readers of JSON values (checkpoints, model answers, stored rows).

Pure, no ORM.
"""

from __future__ import annotations


def positive_int(value: object) -> int | None:
    """``value`` when it is a positive int (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value
