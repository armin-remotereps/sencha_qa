"""Use case: a runner's owner asks their connected Test Runner to update itself.

Locks the runner row first (the order every holder of a runner takes, see
``projects.runner_use``), so an update and a run start, Manual claim or setup
turn on the same runner cannot both pass their "runner is free" checks. The
bundle goes to the runner's current socket only, after commit; the runner
reports its progress with ``update_status`` (``runner_update_progress``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import CustomUser
from controller_client.protocol import CLIENT_VERSION
from projects.controller_protocol import UpdateRunnerEvent
from projects.exceptions import RunnerUpdateRefusedError
from projects.models import RunnerUpdate
from projects.runner_bundle import RunnerBundle, update_bundle
from projects.runner_update_offer import (
    RUNNER_UPDATE_OFFER_FIELDS,
    broadcast_runner_update,
    expire_stale_runner_updates,
    list_runner_update_blockers,
    runner_can_self_update,
    runner_is_older,
)
from projects.runner_use import runner_update_in_flight
from runners.locks import lock_runner_row
from runners.models import TestRunner
from runners.services.runner_access import can_manage
from runners.types import RunnerId

logger = logging.getLogger(__name__)

_STALE_PAGE = (
    "The server was updated since this page was loaded. Reload the page and "
    "try again."
)
_NOT_CONNECTED = "Connect the Test Runner first."
_TOO_OLD = (
    "This Test Runner is too old to update itself. Download and set it up once "
    "from its page; later versions update with one click."
)
_UP_TO_DATE = "The Test Runner is already up to date."
_NOT_YOURS = "Only the Test Runner's owner can update it."
_ALREADY_UPDATING = "An update of the Test Runner is already in progress."
_BUNDLE_UNAVAILABLE = (
    "The update could not be prepared on the server. See the server log."
)


def request_runner_update(
    *, runner_id: RunnerId, user: CustomUser, expected_version: str
) -> RunnerUpdate:
    """Record an update to this server's runner version and send the bundle.

    Owner only (``can_manage``). ``expected_version`` is the version the
    person saw on the page; it must equal the server's, so a page left open
    across a deploy cannot send a bundle the person did not see. Raises
    ``RunnerUpdateRefusedError`` (user-safe) when a gate refuses.
    """
    if expected_version != CLIENT_VERSION:
        raise RunnerUpdateRefusedError(_STALE_PAGE)
    try:
        bundle = update_bundle()
    except (OSError, ValueError):
        logger.exception("Could not build the Test Runner update bundle")
        raise RunnerUpdateRefusedError(_BUNDLE_UNAVAILABLE) from None

    with transaction.atomic():
        locked = lock_runner_row(
            runner_id, fields=(*RUNNER_UPDATE_OFFER_FIELDS, "owner", "archived_at")
        )
        if locked is None or locked.archived_at is not None:
            raise RunnerUpdateRefusedError(_NOT_YOURS)
        if not can_manage(user, locked):
            raise RunnerUpdateRefusedError(_NOT_YOURS)
        now = timezone.now()
        expire_stale_runner_updates(runner_id, now)
        _check_gates(locked, now)
        try:
            with transaction.atomic():
                update = RunnerUpdate.objects.create(
                    runner=locked,
                    requested_by=user,
                    from_version=locked.client_version[:32],
                    to_version=bundle.version,
                    archive_sha256=bundle.sha256,
                )
        except IntegrityError:
            # The one-in-flight constraint; the runner lock makes it unlikely.
            raise RunnerUpdateRefusedError(_ALREADY_UPDATING) from None
        owner = locked.channel_name
        update_id = str(update.pk)
        transaction.on_commit(lambda: _send_bundle(owner, update_id, bundle))
        transaction.on_commit(lambda: broadcast_runner_update(runner_id))
    logger.info(
        "Runner update %s requested for runner %s: %s -> %s",
        update.pk,
        runner_id,
        update.from_version,
        update.to_version,
    )
    return update


def _check_gates(runner: TestRunner, now: datetime) -> None:
    if not runner.connected or not runner.channel_name:
        raise RunnerUpdateRefusedError(_NOT_CONNECTED)
    if not runner_can_self_update(runner.capabilities or []):
        raise RunnerUpdateRefusedError(_TOO_OLD)
    if not runner_is_older(runner.client_version):
        raise RunnerUpdateRefusedError(_UP_TO_DATE)
    blockers = list_runner_update_blockers(runner.runner_id, now)
    if blockers:
        raise RunnerUpdateRefusedError(blockers[0])
    if runner_update_in_flight(runner.runner_id, now):
        raise RunnerUpdateRefusedError(_ALREADY_UPDATING)


def _send_bundle(owner_channel: str, update_id: str, bundle: RunnerBundle) -> None:
    """Send ``update_runner`` to the consumer that owned the runner at commit."""
    layer: Any = get_channel_layer()
    if layer is None:
        logger.error("Runner update %s not sent: no channel layer", update_id)
        return
    event: UpdateRunnerEvent = {
        "type": "controller.update_runner",
        "update_id": update_id,
        "target_version": bundle.version,
        "archive_base64": bundle.archive_base64,
        "archive_sha256": bundle.sha256,
        "archive_size": len(bundle.archive),
    }
    async_to_sync(layer.send)(owner_channel, event)
