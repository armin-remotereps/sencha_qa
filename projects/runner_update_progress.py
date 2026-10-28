"""Use case: record a Test Runner update's progress and its end.

``record_runner_update_status`` stores an ``update_status`` the runner sent;
``settle_runner_update_on_handshake`` ends the in-flight update when a runner
connects: succeeded for its target version, failed when the runner neither
has that version nor says it is still installing it. Both are called by the
controller consumer (sync, through ``sync_to_async``), write the update with
one conditional UPDATE, and push the new state to the Environment page.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

from django.db import transaction
from django.utils import timezone

from controller_client.protocol import UpdatePhase
from projects.case_help_services import restore_open_help_after_runner_update
from projects.models import (
    NON_TERMINAL_RUNNER_UPDATE_STATUSES,
    RUNNER_UPDATE_DETAIL_MAX_LENGTH,
    Project,
    RunnerUpdate,
    RunnerUpdateOldRemoval,
    RunnerUpdateStatus,
)
from projects.runner_update_offer import broadcast_runner_update

logger = logging.getLogger(__name__)

_LOST_UPDATE_DETAIL: Final = (
    "The Test Runner restarted or reconnected without the update; press Update "
    "to try again."
)

# Runner phases of an in-flight update -> server status.
_STATUS_BY_PHASE: Final[dict[UpdatePhase, RunnerUpdateStatus]] = {
    UpdatePhase.ACCEPTED: RunnerUpdateStatus.SENT,
    UpdatePhase.PREPARING: RunnerUpdateStatus.PREPARING,
    UpdatePhase.CREATING_ENVIRONMENT: RunnerUpdateStatus.INSTALLING,
    UpdatePhase.INSTALLING_DEPENDENCIES: RunnerUpdateStatus.INSTALLING,
    UpdatePhase.INSTALLING_BROWSERS: RunnerUpdateStatus.INSTALLING,
    UpdatePhase.STARTING: RunnerUpdateStatus.STARTING,
    UpdatePhase.WAITING_FOR_NEW_RUNNER: RunnerUpdateStatus.WAITING,
    UpdatePhase.FAILED: RunnerUpdateStatus.FAILED,
}

# Sent by the new runner after it connected: accepted on any status (the
# update is normally already succeeded by then).
_OLD_REMOVAL_BY_PHASE: Final[dict[UpdatePhase, RunnerUpdateOldRemoval]] = {
    UpdatePhase.OLD_REMOVED: RunnerUpdateOldRemoval.REMOVED,
    UpdatePhase.OLD_REMOVAL_FAILED: RunnerUpdateOldRemoval.FAILED,
}


def record_runner_update_status(
    project_id: int, update_id: str, phase: UpdatePhase, detail: str
) -> None:
    """Store a phase the project's runner reported for one of its updates.

    Ignores an unknown update, another project's update, and a progress
    phase of an update that already ended (a late or replayed message).
    """
    try:
        update_pk = uuid.UUID(update_id)
    except ValueError:
        logger.warning("Ignoring update_status with invalid id for %s", project_id)
        return
    now = timezone.now()
    detail = detail[:RUNNER_UPDATE_DETAIL_MAX_LENGTH]
    rows = RunnerUpdate.objects.filter(pk=update_pk, project_id=project_id)
    old_removal = _OLD_REMOVAL_BY_PHASE.get(phase)
    if old_removal is not None:
        written = rows.update(old_removal=old_removal, detail=detail, updated_at=now)
    else:
        status = _STATUS_BY_PHASE[phase]
        finished_at = now if status == RunnerUpdateStatus.FAILED else None
        written = rows.filter(status__in=NON_TERMINAL_RUNNER_UPDATE_STATUSES).update(
            status=status,
            phase=phase.value,
            detail=detail,
            finished_at=finished_at,
            updated_at=now,
        )
    if not written:
        logger.info(
            "Ignoring update_status %s for update %s of project %s "
            "(unknown, foreign or already ended)",
            phase.value,
            update_id,
            project_id,
        )
        return
    logger.info(
        "Runner update %s of project %s: %s", update_id, project_id, phase.value
    )
    broadcast_runner_update(project_id)


def settle_runner_update_on_handshake(
    project_id: int, client_version: str, update_in_flight_id: str | None
) -> None:
    """A runner connected: settle the project's in-flight update, if any.

    Only the newest in-flight update counts. A runner of its target version
    ends it as succeeded, and the open current-screen help requests switch to
    restore (the handover closed the old runner's browsers and apps). A runner
    that says it is still installing that update (``update_in_flight_id``)
    leaves it in flight. Any other runner means the update was lost (crash,
    reboot, bundle sent to a dead connection), so it ends as failed rather
    than waiting for expiry.

    Called on every authenticated handshake, after the connection was claimed.
    Locks the project row first, the order the help services use. Always
    broadcasts, so the Connection card's offer follows the runner that just
    connected.
    """
    with transaction.atomic():
        Project.objects.select_for_update().only("id").get(pk=project_id)
        update = (
            RunnerUpdate.objects.filter(
                project_id=project_id,
                status__in=NON_TERMINAL_RUNNER_UPDATE_STATUSES,
            )
            .order_by("-created_at")
            .only("pk", "to_version")
            .first()
        )
        if update is not None:
            _settle(project_id, update, client_version, update_in_flight_id)
        transaction.on_commit(lambda: broadcast_runner_update(project_id))


def _settle(
    project_id: int,
    update: RunnerUpdate,
    client_version: str,
    update_in_flight_id: str | None,
) -> None:
    now = timezone.now()
    in_flight = RunnerUpdate.objects.filter(
        pk=update.pk, status__in=NON_TERMINAL_RUNNER_UPDATE_STATUSES
    )
    if client_version == update.to_version:
        if not in_flight.update(
            status=RunnerUpdateStatus.SUCCEEDED, finished_at=now, updated_at=now
        ):
            return
        switched = restore_open_help_after_runner_update(project_id)
        logger.info(
            "Runner update %s of project %s succeeded: version %s connected "
            "(%d paused case(s) will restore on Continue)",
            update.pk,
            project_id,
            client_version,
            switched,
        )
        return
    if update_in_flight_id == str(update.pk):
        logger.info(
            "Runner of project %s reconnected while installing update %s",
            project_id,
            update.pk,
        )
        return
    if in_flight.update(
        status=RunnerUpdateStatus.FAILED,
        detail=_LOST_UPDATE_DETAIL,
        finished_at=now,
        updated_at=now,
    ):
        logger.warning(
            "Runner update %s of project %s lost: version %s connected "
            "without it (in flight: %s)",
            update.pk,
            project_id,
            client_version or "unknown",
            update_in_flight_id or "none",
        )
