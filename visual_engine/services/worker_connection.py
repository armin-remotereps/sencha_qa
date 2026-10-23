from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from django.db import transaction
from django.utils import timezone

from visual_engine.models import VisionWorker
from visual_engine.services.pool_status import broadcast_visual_engine_status

logger = logging.getLogger(__name__)


def claim_worker(
    *,
    name: str,
    channel_name: str,
    worker_version: str,
    capabilities: Sequence[str],
    system_info: Mapping[str, str | int],
) -> str:
    """Make ``channel_name`` the owner of the worker row called ``name``.

    The newest authenticated connection always wins. Returns the channel name
    of the connection it replaced (so the caller can close it), or "" when
    there was none. The row is created on the first connection and reused on
    every reconnect; its state resets until the worker reports again.
    """
    now = timezone.now()
    fields = {
        "connected": True,
        "channel_name": channel_name,
        "state": "",
        "status_message": "",
        "device": "",
        "load_seconds": None,
        "client_version": worker_version[:32],
        "capabilities": list(capabilities),
        "system_info": dict(system_info),
        "connected_at": now,
        "last_seen_at": now,
    }
    with transaction.atomic():
        worker, created = VisionWorker.objects.select_for_update().get_or_create(
            name=name, defaults=fields
        )
        previous_channel_name = "" if created else worker.channel_name
        if not created:
            VisionWorker.objects.filter(pk=worker.pk).update(**fields)
        transaction.on_commit(broadcast_visual_engine_status)

    if previous_channel_name == channel_name:
        return ""
    return previous_channel_name


def release_worker(*, name: str, channel_name: str) -> bool:
    """Mark the worker disconnected only if ``channel_name`` still owns it.

    Returns True when the row changed (and a broadcast was scheduled), False
    when a newer connection already took over.
    """
    rows_updated = VisionWorker.objects.filter(
        name=name, channel_name=channel_name
    ).update(
        connected=False,
        channel_name="",
        state="",
        last_disconnected_at=timezone.now(),
    )
    if not rows_updated:
        return False
    # Runs at once in autocommit; deferred if a caller holds a transaction.
    transaction.on_commit(broadcast_visual_engine_status)
    return True
