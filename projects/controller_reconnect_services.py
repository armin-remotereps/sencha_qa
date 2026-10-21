"""Wait for the Test Runner during an active run, within a bounded grace period.

Used by the controller action dispatch in ``projects.services`` before every
action and after a lost connection. It must not import ``projects.services``.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings

from agents.types import AgentCancelledError
from projects.controller_outage_clock import outage_seconds, record_outage
from projects.exceptions import (
    ControllerNotConnectedError,
    ControllerReconnectTimeoutError,
)
from projects.models import Project, TestRun, TestRunStatus

logger = logging.getLogger(__name__)

CONTROLLER_POLL_INTERVAL_SECONDS = 2


def max_outage_seconds_per_case() -> int:
    """Total reconnect-wait allowance for one test case (also added to Celery limits)."""
    return 2 * settings.CONTROLLER_RECONNECT_GRACE_SECONDS


def wait_for_controller(project_id: int, lost_owner: str = "") -> str:
    """Return the owning consumer channel, waiting for a reconnect if needed.

    Waits only while the project has a STARTED run, for at most the grace
    period and the remaining per-case allowance. The time actually spent
    waiting is recorded on the outage clock so agent deadlines skip it.

    ``lost_owner`` is a channel already known to be dead: it never counts as
    connected, even if its consumer has not released the project yet.
    """
    owner = connected_owner(project_id, lost_owner)
    if owner is not None:
        return owner

    if not _has_started_run(project_id):
        raise ControllerNotConnectedError(
            f"Test Runner is not connected for project {project_id}"
        )

    grace = settings.CONTROLLER_RECONNECT_GRACE_SECONDS
    budget = min(float(grace), max_outage_seconds_per_case() - outage_seconds())
    if budget <= 0:
        raise ControllerReconnectTimeoutError(grace)

    logger.info(
        "Test Runner disconnected for project %d; waiting up to %.0fs to reconnect",
        project_id,
        budget,
    )
    start = time.monotonic()
    try:
        return _poll_for_reconnect(project_id, lost_owner, start, budget, grace)
    finally:
        waited = time.monotonic() - start
        record_outage(waited)
        logger.info(
            "Stopped waiting for Test Runner of project %d after %.1fs",
            project_id,
            waited,
        )


def controller_connection_lost(project_id: int, owner_channel: str) -> bool:
    """True when the connection that received an action no longer owns the project."""
    row = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_connected", "agent_channel_name")
        .first()
    )
    if row is None:
        return True
    connected, channel_name = row
    return not connected or channel_name != owner_channel


def _poll_for_reconnect(
    project_id: int, lost_owner: str, start: float, budget: float, grace: int
) -> str:
    while True:
        remaining = budget - (time.monotonic() - start)
        if remaining <= 0:
            raise ControllerReconnectTimeoutError(grace)
        time.sleep(min(float(CONTROLLER_POLL_INTERVAL_SECONDS), remaining))

        owner = connected_owner(project_id, lost_owner)
        if owner is not None:
            logger.info("Test Runner reconnected for project %d", project_id)
            return owner
        if not _has_started_run(project_id):
            raise AgentCancelledError(
                "Test run cancelled while waiting for the Test Runner to reconnect"
            )


def connected_owner(project_id: int, lost_owner: str = "") -> str | None:
    """The owning consumer channel right now, or None; never waits."""
    owners = Project.objects.filter(pk=project_id, agent_connected=True)
    if lost_owner:
        owners = owners.exclude(agent_channel_name=lost_owner)
    return owners.values_list("agent_channel_name", flat=True).first()


def _has_started_run(project_id: int) -> bool:
    return TestRun.objects.filter(
        project_id=project_id, status=TestRunStatus.STARTED
    ).exists()
