"""Record the device status a phone or tablet Test Runner reports.

docs/plan/feat-mobile-device-runner.md (D6): the runner sends
``device_status`` on every change and every 30 s; the server keeps it on the
runner row, applied only for the connection that owns the runner.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from controller_client.protocol import DeviceState
from projects.models import RunnerDeviceState
from runners.models import TestRunner
from runners.services.runner_status import broadcast_runner_status
from runners.types import RunnerId


def record_device_status(
    runner_id: RunnerId, channel_name: str, state: DeviceState, detail: str
) -> bool:
    """Store the device status; return whether the pages must be told.

    True when the state or detail changed, or when the last report had gone
    stale (the pages showed the device offline). Always refreshes
    ``device_reported_at``. At most two conditional UPDATEs and no read; the
    usual heartbeat is one. Both apply only while ``channel_name`` still owns
    the runner (a superseded connection writes nothing) and only to a phone
    runner (a blank device state is never written).
    """
    now = timezone.now()
    fresh_since = now - timedelta(seconds=settings.RUNNER_DEVICE_STALE_SECONDS)
    owned = TestRunner.objects.filter(
        pk=runner_id, channel_name=channel_name, connected=True
    ).exclude(device_state=RunnerDeviceState.NONE)
    unchanged = owned.filter(
        device_state=state.value,
        device_detail=detail,
        device_reported_at__gte=fresh_since,
    ).update(device_reported_at=now)
    if unchanged:
        return False
    changed = owned.update(
        device_state=state.value,
        device_detail=detail,
        device_reported_at=now,
        updated_at=now,
    )
    return changed > 0


def broadcast_device_status(runner_id: RunnerId) -> None:
    """Send ``runner.status`` after a device status change (one query)."""
    runner = TestRunner.objects.filter(pk=runner_id).first()
    if runner is not None:
        broadcast_runner_status(runner)
