"""Force disconnect a runner (its owner, or an active superuser: Q8)."""

from __future__ import annotations

from accounts.models import CustomUser
from runners.exceptions import RunnerNotFoundError
from runners.models import TestRunner
from runners.services.runner_access import can_force_disconnect
from runners.services.runner_connection import force_disconnect_runner
from runners.types import RunnerId


def disconnect_runner(*, actor: CustomUser, runner_id: RunnerId) -> bool:
    """Close the runner's socket and abort the agent runs it executes.

    Returns False when it was not connected. Raises ``RunnerNotFoundError``
    for a missing runner or one the actor may not disconnect (the same
    answer, so another user's runner is not revealed).
    """
    runner = TestRunner.objects.filter(pk=runner_id).first()
    if runner is None or not can_force_disconnect(actor, runner):
        raise RunnerNotFoundError()
    return force_disconnect_runner(runner)
