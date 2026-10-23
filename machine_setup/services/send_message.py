"""Use case: a member sends a chat message, which starts one agent turn."""

from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.events import broadcast_message, broadcast_turn
from machine_setup.exceptions import (
    MachineSetupError,
    SetupMachineNotReadyError,
    SetupRunActiveError,
    SetupTurnActiveError,
)
from machine_setup.models import (
    SetupConversation,
    SetupMessage,
    SetupMessageKind,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.payloads import message_payload, turn_payload
from machine_setup.services.live_turn import resolve_live_turn
from machine_setup.tasks import run_setup_turn
from projects.models import Project, TestRun, TestRunStatus
from projects.services import MachineState, get_machine_state

logger = logging.getLogger(__name__)

ENQUEUE_FAILED_MESSAGE = "The setup agent could not be started. Try again."


def send_setup_message(*, project: Project, user: CustomUser, text: str) -> SetupTurn:
    """Record the user's message and queue a turn for it.

    Raises ``SetupTurnActiveError``, ``SetupRunActiveError`` or
    ``SetupMachineNotReadyError``. A stale live turn is closed as
    ``worker_lost`` first, and stays closed even when the send is refused.
    """
    outcome = _send_locked(project=project, user=user, text=text)
    if isinstance(outcome, MachineSetupError):
        raise outcome
    return outcome


@transaction.atomic
def _send_locked(
    *, project: Project, user: CustomUser, text: str
) -> SetupTurn | MachineSetupError:
    # Same lock as start_test_run and resume (project row first), so a send
    # and a run start cannot both pass their "nothing else is live" checks.
    locked = (
        Project.objects.select_for_update()
        .only("id", "agent_connected", "agent_omniparser_status")
        .get(pk=project.pk)
    )
    refusal = _refusal(locked, timezone.now())
    if refusal is not None:
        # Returned, not raised: a stale turn closed above must still commit.
        return refusal
    turn, message = _create_turn(project=locked, user=user, text=text)
    transaction.on_commit(lambda: _broadcast_and_enqueue(turn, message))
    return turn


def _refusal(project: Project, now: datetime) -> MachineSetupError | None:
    if resolve_live_turn(project_id=project.pk, now=now) is not None:
        return SetupTurnActiveError()
    if TestRun.objects.filter(project=project, status=TestRunStatus.STARTED).exists():
        return SetupRunActiveError()
    if get_machine_state(project) != MachineState.READY:
        return SetupMachineNotReadyError()
    return None


def _create_turn(
    *, project: Project, user: CustomUser, text: str
) -> tuple[SetupTurn, SetupMessage]:
    conversation = (
        SetupConversation.objects.filter(project=project, archived_at__isnull=True)
        .defer("llm_transcript")
        .first()
    )
    if conversation is None:
        conversation = SetupConversation.objects.create(
            project=project, created_by=user
        )
    turn = SetupTurn.objects.create(
        conversation=conversation, project=project, requested_by=user
    )
    message = SetupMessage.objects.create(
        conversation=conversation,
        turn=turn,
        kind=SetupMessageKind.USER,
        author=user,
        text=text,
    )
    return turn, message


def _broadcast_and_enqueue(turn: SetupTurn, message: SetupMessage) -> None:
    # Broadcast the queued turn before enqueueing, so a fast worker's
    # "running" broadcast cannot be overtaken by a stale "queued" one.
    broadcast_message(turn.project_id, message_payload(message, screenshots=[]))
    broadcast_turn(turn.project_id, turn_payload(turn))
    try:
        result = run_setup_turn.delay(turn.pk)
    except Exception:
        logger.exception("Could not queue setup turn %d", turn.pk)
        _fail_unqueued_turn(turn)
        return
    SetupTurn.objects.filter(pk=turn.pk).update(celery_task_id=result.id or "")


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
