"""Record the device status a phone or tablet Test Runner reports.

docs/plan/feat-mobile-device-runner.md (D6): the runner sends
``device_status`` on every change and every 30 s; the server keeps it on the
project, applied only for the connection that owns the project.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from controller_client.protocol import DeviceState
from projects.models import Project, RunnerDeviceState
from projects.services import broadcast_agent_status


def record_device_status(
    project_id: int, channel_name: str, state: DeviceState, detail: str
) -> bool:
    """Store the device status; return whether the page must be told.

    True when the state or detail changed, or when the last report had gone
    stale (the page showed the device offline). Always refreshes
    ``agent_device_reported_at``. At most two conditional UPDATEs and no
    read; the usual heartbeat is one. Both apply only while ``channel_name``
    still owns the project (a superseded connection writes nothing) and only
    to a phone runner (a blank device state is never written).
    """
    now = timezone.now()
    fresh_since = now - timedelta(seconds=settings.RUNNER_DEVICE_STALE_SECONDS)
    owned = Project.objects.filter(
        pk=project_id, agent_channel_name=channel_name, agent_connected=True
    ).exclude(agent_device_state=RunnerDeviceState.NONE)
    unchanged = owned.filter(
        agent_device_state=state.value,
        agent_device_detail=detail,
        agent_device_reported_at__gte=fresh_since,
    ).update(agent_device_reported_at=now)
    if unchanged:
        return False
    changed = owned.update(
        agent_device_state=state.value,
        agent_device_detail=detail,
        agent_device_reported_at=now,
    )
    return changed > 0


def broadcast_device_status(project_id: int) -> None:
    """Send ``agent.status`` after a device status change (one query)."""
    project = Project.objects.filter(pk=project_id).first()
    if project is not None:
        broadcast_agent_status(project)
