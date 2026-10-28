"""Tell the project's Test Runner connection that its person steps may have changed.

The event carries no payload, so no case text goes onto the channel layer:
the controller consumer (the current owner only, through the group) re-reads
the open steps itself and pushes a ``person_step_state`` when they changed
(``runner_person_step_services.get_runner_person_steps``). A leaf module with
no model imports, so ``projects.services`` can call it.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

# Channels event type; ``ControllerConsumer.controller_person_steps_changed``.
PERSON_STEPS_CHANGED_EVENT = "controller.person_steps_changed"


def nudge_runner_person_steps(project_id: int) -> None:
    """Nudge the project's controller group. Call it after commit; never raises."""
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            f"controller_{project_id}", {"type": PERSON_STEPS_CHANGED_EVENT}
        )
    except Exception:
        logger.exception("Person step nudge failed for project %s", project_id)
