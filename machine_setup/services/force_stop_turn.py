"""Use case: a member force stops a running turn whose Stop went unanswered."""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from auto_tester.celery import app as celery_app
from machine_setup.exceptions import (
    SetupForceStopNotAllowedError,
    SetupNoActiveTurnError,
)
from machine_setup.force_stop_policy import force_stop_in
from machine_setup.models import (
    LIVE_TURN_STATUSES,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.services.live_turn import close_live_turn, resolve_live_turn
from projects.models import Project

logger = logging.getLogger(__name__)

FORCE_STOPPED_NOTICE = (
    "Force stopped. The agent's last action may still finish on the test "
    "machine; check it before carrying on."
)
PRESS_STOP_FIRST_MESSAGE = "Press Stop first."


def force_stop_turn(*, project: Project, user: CustomUser, turn_id: int) -> None:
    """Close a running turn at once, after Stop and its grace period.

    The turn is closed in the database (tool rows failed, notice, broadcast,
    auto-fix settled) and its Celery task is revoked on commit. Raises
    ``SetupNoActiveTurnError`` when the turn is not live (a stale one is
    closed as ``worker_lost`` first) and ``SetupForceStopNotAllowedError``
    before Stop or inside the grace period.
    """
    with transaction.atomic():
        Project.objects.select_for_update().only("id").get(pk=project.pk)
        now = timezone.now()
        resolve_live_turn(project_id=project.pk, now=now)
        turn = (
            SetupTurn.objects.select_for_update()
            .filter(pk=turn_id, project=project)
            .first()
        )
        if turn is None or turn.status not in LIVE_TURN_STATUSES:
            raise SetupNoActiveTurnError()
        wait = force_stop_in(turn, now)
        if wait is None:
            # A queued turn is closed by Stop itself, so only "not flagged" is left.
            raise SetupForceStopNotAllowedError(PRESS_STOP_FIRST_MESSAGE)
        if wait > 0:
            raise SetupForceStopNotAllowedError(
                f"Force stop is possible in {wait} s. Give the agent a moment "
                "to stop on its own."
            )
        closed = close_live_turn(
            turn,
            now,
            status=SetupTurnStatus.STOPPED,
            ended_reason=SetupTurnEndReason.FORCE_STOPPED,
            error_message="",
            notice_text=FORCE_STOPPED_NOTICE,
            require_status=(SetupTurnStatus.RUNNING,),
        )
        if not closed:
            raise SetupNoActiveTurnError()
        logger.info(
            "Force stopped setup turn %s of project %s (user %s)",
            turn.pk,
            project.pk,
            user.pk,
        )
        task_id = turn.celery_task_id
        if task_id:
            transaction.on_commit(lambda: _revoke(task_id, turn_id=turn.pk))


def _revoke(task_id: str, *, turn_id: int) -> None:
    """Terminate the turn's worker child; never raises (the close stands alone)."""
    try:
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    except Exception:  # Broker trouble must not fail the request.
        logger.exception("Could not revoke the task of force stopped turn %s", turn_id)
