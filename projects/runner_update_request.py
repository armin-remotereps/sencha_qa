"""Use case: a project member asks the connected Test Runner to update itself.

Locks the project row first (the order ``start_test_run``, resume and machine
setup sends use), so an update and a run start or setup turn cannot both pass
their "machine is free" checks. The bundle goes to the current owner channel
only, after commit; the runner reports its progress with ``update_status``
(``runner_update_progress``).
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
from projects.models import Project, RunnerUpdate
from projects.runner_bundle import RunnerBundle, update_bundle
from projects.runner_update_offer import (
    broadcast_runner_update,
    list_runner_update_blockers,
    runner_can_self_update,
    runner_is_older,
)
from projects.runner_update_state import (
    expire_stale_runner_updates,
    runner_update_in_progress,
)

logger = logging.getLogger(__name__)

_STALE_PAGE = (
    "The server was updated since this page was loaded. Reload the page and "
    "try again."
)
_NOT_CONNECTED = "Connect the Test Runner first."
_TOO_OLD = (
    "This Test Runner is too old to update itself. Download and set it up once "
    "from this page; later versions update with one click."
)
_UP_TO_DATE = "The Test Runner is already up to date."
_ALREADY_UPDATING = "An update of the Test Runner is already in progress."
_BUNDLE_UNAVAILABLE = (
    "The update could not be prepared on the server. See the server log."
)


def request_runner_update(
    *, project: Project, user: CustomUser, expected_version: str
) -> RunnerUpdate:
    """Record an update to this server's runner version and send the bundle.

    ``expected_version`` is the version the person saw on the page; it must
    equal the server's, so a page left open across a deploy cannot send a
    bundle the person did not see. Raises ``RunnerUpdateRefusedError``
    (user-safe) when a gate refuses.
    """
    if expected_version != CLIENT_VERSION:
        raise RunnerUpdateRefusedError(_STALE_PAGE)
    try:
        bundle = update_bundle()
    except (OSError, ValueError):
        logger.exception("Could not build the Test Runner update bundle")
        raise RunnerUpdateRefusedError(_BUNDLE_UNAVAILABLE) from None

    with transaction.atomic():
        locked = (
            Project.objects.select_for_update()
            .only(
                "id",
                "agent_connected",
                "agent_channel_name",
                "agent_client_version",
                "agent_capabilities",
            )
            .get(pk=project.pk)
        )
        now = timezone.now()
        expire_stale_runner_updates(locked.id, now)
        _check_gates(locked, now)
        try:
            with transaction.atomic():
                update = RunnerUpdate.objects.create(
                    project=locked,
                    requested_by=user,
                    from_version=locked.agent_client_version[:32],
                    to_version=bundle.version,
                    archive_sha256=bundle.sha256,
                )
        except IntegrityError:
            # The one-in-flight constraint; the project lock makes it unlikely.
            raise RunnerUpdateRefusedError(_ALREADY_UPDATING) from None
        owner = locked.agent_channel_name
        update_id = str(update.pk)
        transaction.on_commit(lambda: _send_bundle(owner, update_id, bundle))
        transaction.on_commit(lambda: broadcast_runner_update(locked.id))
    logger.info(
        "Runner update %s requested for project %s: %s -> %s",
        update.pk,
        locked.id,
        update.from_version,
        update.to_version,
    )
    return update


def _check_gates(project: Project, now: datetime) -> None:
    if not project.agent_connected or not project.agent_channel_name:
        raise RunnerUpdateRefusedError(_NOT_CONNECTED)
    if not runner_can_self_update(project.agent_capabilities):
        raise RunnerUpdateRefusedError(_TOO_OLD)
    if not runner_is_older(project.agent_client_version):
        raise RunnerUpdateRefusedError(_UP_TO_DATE)
    blockers = list_runner_update_blockers(project.id, now)
    if blockers:
        raise RunnerUpdateRefusedError(blockers[0])
    if runner_update_in_progress(project.id, now):
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
