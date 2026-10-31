"""Live ``machine_readiness`` events on the project's Machine setup socket.

A leaf module: ``machine_readiness`` must not import ``machine_setup``, so the
setup socket's group name is repeated here. It must stay equal to
``machine_setup.events.setup_group``; ``machine_setup.consumers`` handles the
``machine_setup.readiness`` event type and forwards it to the page as
``{"type": "machine_readiness", "readiness": {...}}``.
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

READINESS_EVENT_TYPE = "machine_setup.readiness"


def setup_socket_group(project_id: int) -> str:
    """The Machine setup socket's group (same string as machine_setup.events)."""
    return f"machine_setup_{project_id}"


def broadcast_readiness(project_id: int, payload: dict[str, object]) -> None:
    """Send one readiness event (``payload["event"]``: profile, intake_skipped, scan or
    analysis) to the setup page. Never raises: a missed live update only
    means the page shows it on its next load."""
    try:
        layer: Any = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            setup_socket_group(project_id),
            {"type": READINESS_EVENT_TYPE, "readiness": payload},
        )
    except Exception:
        logger.exception("Machine readiness event for project %s failed", project_id)
