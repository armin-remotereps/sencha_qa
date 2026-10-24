from __future__ import annotations

from django.conf import settings

from agents.timeout_bounds import TimeoutBounds, resolve_timeout_seconds

__all__ = ["TimeoutBounds", "build_timeout_bounds", "resolve_timeout_seconds"]


def build_timeout_bounds() -> TimeoutBounds:
    return TimeoutBounds(
        default=settings.SUB_AGENT_TIMEOUT_SECONDS,
        minimum=settings.SUB_AGENT_MIN_TIMEOUT_SECONDS,
        maximum=settings.SUB_AGENT_MAX_TIMEOUT_SECONDS,
    )
