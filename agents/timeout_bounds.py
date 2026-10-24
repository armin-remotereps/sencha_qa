"""Sub-task timeout bounds (pure; no Django, no ``agents.services`` import)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeoutBounds:
    default: int
    minimum: int
    maximum: int


def resolve_timeout_seconds(raw: object, bounds: TimeoutBounds) -> int:
    requested = _parse_whole_seconds(raw)
    if requested is None:
        requested = bounds.default
    return max(bounds.minimum, min(bounds.maximum, requested))


def _parse_whole_seconds(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(float(raw))
        except ValueError:
            return None
    return None
