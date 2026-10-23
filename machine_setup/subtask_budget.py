"""Wall-clock budget of one setup sub-task."""

from __future__ import annotations

from django.conf import settings

from agents.services.sub_task_budget import TimeoutBounds, resolve_timeout_seconds

# Turn time kept back after a sub-task so the main agent can still reply.
MAIN_REPLY_RESERVE_SECONDS = 60


def build_subtask_bounds() -> TimeoutBounds:
    return TimeoutBounds(
        default=settings.MACHINE_SETUP_SUBTASK_TIMEOUT_SECONDS,
        minimum=settings.MACHINE_SETUP_SUBTASK_MIN_TIMEOUT_SECONDS,
        maximum=settings.MACHINE_SETUP_SUBTASK_MAX_TIMEOUT_SECONDS,
    )


def resolve_subtask_budget(
    raw: object, *, bounds: TimeoutBounds, deadline: float | None, now: float
) -> int | None:
    """The sub-task's budget in seconds, or ``None`` when the turn is nearly over.

    The requested budget is clamped to ``bounds``, then to what the turn has
    left minus the main agent's reply reserve.
    """
    budget = resolve_timeout_seconds(raw, bounds)
    if deadline is None:
        return budget
    left = int(deadline - now) - MAIN_REPLY_RESERVE_SECONDS
    if left < bounds.minimum:
        return None
    return min(budget, left)
