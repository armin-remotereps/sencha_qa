"""Create a setup turn with its user message, then broadcast and enqueue it.

Shared by a person's send (``send_message``) and the server-started auto-fix
(``case_auto_fix``). The turn is enqueued by task name so this module does not
import ``machine_setup.tasks``, which imports the worker runtime (and through
it this package): a direct import would be a cycle.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from auto_tester.celery import app as celery_app
from machine_setup.events import broadcast_message, broadcast_turn
from machine_setup.models import (
    SetupConversation,
    SetupMessage,
    SetupMessageKind,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.payloads import message_payload, turn_payload

logger = logging.getLogger(__name__)

# The name `machine_setup.tasks.run_setup_turn` is registered under; routed to
# the `execution` queue by CELERY_TASK_ROUTES.
RUN_SETUP_TURN_TASK = "machine_setup.tasks.run_setup_turn"

ENQUEUE_FAILED_MESSAGE = "The setup agent could not be started. Try again."


def create_turn(
    *,
    conversation: SetupConversation,
    requested_by: CustomUser | None,
    text: str,
    auto_fix: bool = False,
) -> tuple[SetupTurn, SetupMessage]:
    """A queued turn plus its USER row; call under the project row lock."""
    turn = SetupTurn.objects.create(
        conversation=conversation,
        project_id=conversation.project_id,
        requested_by=requested_by,
        auto_fix=auto_fix,
    )
    message = SetupMessage.objects.create(
        conversation=conversation,
        turn=turn,
        kind=SetupMessageKind.USER,
        author=requested_by,
        text=text,
    )
    return turn, message


def broadcast_and_enqueue(turn: SetupTurn, message: SetupMessage) -> bool:
    """Run on commit. Returns False when the task could not be queued.

    The turn is then closed as ``failed/internal_error`` with a notice row.
    """
    # Broadcast the queued turn before enqueueing, so a fast worker's
    # "running" broadcast cannot be overtaken by a stale "queued" one.
    broadcast_message(turn.project_id, message_payload(message, screenshots=[]))
    broadcast_turn(turn.project_id, turn_payload(turn))
    try:
        result = celery_app.send_task(RUN_SETUP_TURN_TASK, args=[turn.pk])
    except Exception:
        logger.exception("Could not queue setup turn %d", turn.pk)
        _fail_unqueued_turn(turn)
        return False
    SetupTurn.objects.filter(pk=turn.pk).update(celery_task_id=result.id or "")
    return True


def _fail_unqueued_turn(turn: SetupTurn) -> None:
    now = timezone.now()
    with transaction.atomic():
        failed = SetupTurn.objects.filter(
            pk=turn.pk, status=SetupTurnStatus.QUEUED
        ).update(
            status=SetupTurnStatus.FAILED,
            ended_reason=SetupTurnEndReason.INTERNAL_ERROR,
            error_message=ENQUEUE_FAILED_MESSAGE,
            finished_at=now,
        )
        if not failed:
            return
        notice = SetupMessage.objects.create(
            conversation_id=turn.conversation_id,
            turn=turn,
            kind=SetupMessageKind.NOTICE,
            text=ENQUEUE_FAILED_MESSAGE,
        )
    turn.status = SetupTurnStatus.FAILED
    turn.ended_reason = SetupTurnEndReason.INTERNAL_ERROR
    turn.error_message = ENQUEUE_FAILED_MESSAGE
    turn.finished_at = now
    broadcast_message(turn.project_id, message_payload(notice, screenshots=[]))
    broadcast_turn(turn.project_id, turn_payload(turn))
