"""Tell a Test Runner's connection that its person steps may have changed.

The event carries no payload, so no case text goes onto the channel layer:
the controller consumer (the current owner only, through the group) re-reads
the open steps of the runs assigned to its runner and pushes a
``person_step_state`` when they changed. A leaf module with no model imports,
so ``projects.services`` can call it. Callers pass the run's runner; a
reassign nudges both the old and the new runner.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from runners.channels import runner_group
from runners.types import RunnerId

logger = logging.getLogger(__name__)

# Channels event type; ``ControllerConsumer.controller_person_steps_changed``.
PERSON_STEPS_CHANGED_EVENT = "controller.person_steps_changed"


def nudge_runner_person_steps(runner_id: RunnerId | None) -> None:
    """Nudge the runner's controller group (``runner_{id}``).

    Call it after commit; never raises. ``None`` (a run with no runner) is a
    no-op, so callers can pass ``run.runner_id`` as it is.
    """
    if runner_id is None:
        return
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            runner_group(runner_id), {"type": PERSON_STEPS_CHANGED_EVENT}
        )
    except Exception:
        logger.exception("Person step nudge failed for runner %s", runner_id)
