"""Format a duration for display (pure)."""

from __future__ import annotations

from datetime import timedelta

UNKNOWN_DURATION = "—"


def format_duration(value: timedelta | None) -> str:
    """A short label: "1h 02m", "14m 32s", "45s"; "< 1s" under a second; "—" for None.

    Hours drop the seconds; a negative value (clock skew) counts as zero.
    """
    if value is None:
        return UNKNOWN_DURATION
    total_seconds = max(0, int(value.total_seconds()))
    if total_seconds == 0:
        return "< 1s"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"
