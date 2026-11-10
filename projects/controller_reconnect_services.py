"""Wait for the Test Runner during an active run, within a bounded grace period.

Used by the controller action dispatch in ``projects.services`` before every
action and after a lost connection; keyed by Test Runner (``RunnerId``). It must not import ``projects.services``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings

from agents.types import AgentCancelledError
from projects.controller_outage_clock import outage_seconds, record_outage
from projects.exceptions import (
    ControllerNotConnectedError,
    ControllerReconnectTimeoutError,
)
from projects.models import TestRun, TestRunStatus
from runners.models import TestRunner
from runners.types import RunnerId

logger = logging.getLogger(__name__)

CONTROLLER_POLL_INTERVAL_SECONDS = 2

# Set by callers that must fail at once instead of waiting for a reconnect
# (bounded calls that answer a person, and their clean-up tasks).
_no_reconnect_wait: ContextVar[bool] = ContextVar("no_reconnect_wait", default=False)


@contextmanager
def no_reconnect_wait() -> Iterator[None]:
    """While active, ``wait_for_controller`` never waits for a reconnect.

    With no owner connected it raises ControllerNotConnectedError at once and
    records no outage, even while a run is Started.
    """
    token = _no_reconnect_wait.set(True)
    try:
        yield
    finally:
        _no_reconnect_wait.reset(token)


def max_outage_seconds_per_case() -> int:
    """Total reconnect-wait allowance for one test case (also added to Celery limits)."""
    return 2 * settings.CONTROLLER_RECONNECT_GRACE_SECONDS


def wait_for_controller(runner_id: RunnerId, lost_owner: str = "") -> str:
    """Return the owning consumer channel, waiting for a reconnect if needed.

    Waits only while a STARTED run is assigned to the runner, for at most the grace
    period and the remaining per-case allowance. The time actually spent
    waiting is recorded on the outage clock so agent deadlines skip it.

    ``lost_owner`` is a channel already known to be dead: it never counts as
    connected, even if its consumer has not released the runner yet.
    """
    owner = connected_owner(runner_id, lost_owner)
    if owner is not None:
        return owner

    if _no_reconnect_wait.get() or not _has_started_run(runner_id):
        raise ControllerNotConnectedError(
            f"Test Runner is not connected for runner {runner_id}"
        )

    grace = settings.CONTROLLER_RECONNECT_GRACE_SECONDS
    budget = min(float(grace), max_outage_seconds_per_case() - outage_seconds())
    if budget <= 0:
        raise ControllerReconnectTimeoutError(grace)

    logger.info(
        "Test Runner disconnected for runner %d; waiting up to %.0fs to reconnect",
        runner_id,
        budget,
    )
    start = time.monotonic()
    try:
        return _poll_for_reconnect(runner_id, lost_owner, start, budget, grace)
    finally:
        waited = time.monotonic() - start
        record_outage(waited)
        logger.info(
            "Stopped waiting for Test Runner of runner %d after %.1fs",
            runner_id,
            waited,
        )


def controller_connection_lost(runner_id: RunnerId, owner_channel: str) -> bool:
    """True when the connection that received an action no longer owns the runner."""
    row = (
        TestRunner.objects.filter(pk=runner_id)
        .values_list("connected", "channel_name")
        .first()
    )
    if row is None:
        return True
    connected, channel_name = row
    return not connected or channel_name != owner_channel


def _poll_for_reconnect(
    runner_id: RunnerId, lost_owner: str, start: float, budget: float, grace: int
) -> str:
    while True:
        remaining = budget - (time.monotonic() - start)
        if remaining <= 0:
            raise ControllerReconnectTimeoutError(grace)
        time.sleep(min(float(CONTROLLER_POLL_INTERVAL_SECONDS), remaining))

        owner = connected_owner(runner_id, lost_owner)
        if owner is not None:
            logger.info("Test Runner reconnected for runner %d", runner_id)
            return owner
        if not _has_started_run(runner_id):
            raise AgentCancelledError(
                "Test run cancelled while waiting for the Test Runner to reconnect"
            )


def connected_owner(runner_id: RunnerId, lost_owner: str = "") -> str | None:
    """The owning consumer channel right now, or None; never waits."""
    owners = TestRunner.objects.filter(pk=runner_id, connected=True)
    if lost_owner:
        owners = owners.exclude(channel_name=lost_owner)
    return owners.values_list("channel_name", flat=True).first()


def _has_started_run(runner_id: RunnerId) -> bool:
    """A STARTED run (any mode) is assigned to the runner."""
    return TestRun.objects.filter(
        runner_id=runner_id, status=TestRunStatus.STARTED
    ).exists()
