from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vision_worker.protocol import StatusPayload
from visual_engine.models import VisionWorker
from visual_engine.services.pool_status import broadcast_visual_engine_status

_MAX_MESSAGE_LENGTH = 500
_MAX_DEVICE_LENGTH = 64


def record_worker_status(
    *, name: str, channel_name: str, status: StatusPayload
) -> None:
    """Store a worker's status report (also its heartbeat).

    Both updates are filtered on the owning channel, so a superseded socket
    writes nothing. Only a state change is broadcast; heartbeats only bump
    ``last_seen_at`` and never reach the UI.
    """
    now = timezone.now()
    message = status.message[:_MAX_MESSAGE_LENGTH]
    owned = VisionWorker.objects.filter(name=name, channel_name=channel_name)
    changed = owned.exclude(state=status.state).update(
        state=status.state,
        status_message=message,
        device=status.device[:_MAX_DEVICE_LENGTH],
        load_seconds=status.load_seconds,
        last_seen_at=now,
    )
    if changed:
        # Runs at once in autocommit; deferred if a caller holds a transaction.
        transaction.on_commit(broadcast_visual_engine_status)
        return
    owned.update(last_seen_at=now, status_message=message)
