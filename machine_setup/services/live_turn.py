"""The project's live turn: find it (closing it when its worker is gone) and stop it.

Shared by send, stop and reset, which all run under the project row lock.
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.events import broadcast_message, broadcast_turn
from machine_setup.models import (
    LIVE_TURN_STATUSES,
    SetupMessage,
    SetupMessageKind,
    SetupToolStatus,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.payloads import message_payload, turn_payload
from machine_setup.turn_outcome import stopped_outcome
from machine_setup.turn_state import is_turn_stale

WORKER_LOST_MESSAGE = (
    "The setup agent stopped responding and its turn was closed. "
    "Send a message to carry on."
)


def resolve_live_turn(*, project_id: int, now: datetime) -> SetupTurn | None:
    """The project's queued or running turn, or ``None``.

    A stale live turn (its worker was killed) is closed as ``failed/worker_lost``
    with a notice row, broadcast on commit, and ``None`` is returned. Call it in
    a transaction holding the project row lock.
    """
    turn = (
        SetupTurn.objects.filter(project_id=project_id, status__in=LIVE_TURN_STATUSES)
        .order_by("-id")
        .first()
    )
    if turn is None or not is_turn_stale(turn, now):
        return turn
    _close_lost_turn(turn, now)
    return None


def _close_lost_turn(turn: SetupTurn, now: datetime) -> None:
    closed = SetupTurn.objects.filter(pk=turn.pk, status__in=LIVE_TURN_STATUSES).update(
        status=SetupTurnStatus.FAILED,
        ended_reason=SetupTurnEndReason.WORKER_LOST,
        error_message=WORKER_LOST_MESSAGE,
        finished_at=now,
    )
    if not closed:
        return
    turn.status = SetupTurnStatus.FAILED
    turn.ended_reason = SetupTurnEndReason.WORKER_LOST
    turn.error_message = WORKER_LOST_MESSAGE
    turn.finished_at = now
    SetupMessage.objects.filter(turn=turn, tool_status=SetupToolStatus.RUNNING).update(
        tool_status=SetupToolStatus.FAILED
    )
    notice = SetupMessage.objects.create(
        conversation_id=turn.conversation_id,
        turn=turn,
        kind=SetupMessageKind.NOTICE,
        text=WORKER_LOST_MESSAGE,
    )

    def _broadcast() -> None:
        broadcast_message(turn.project_id, message_payload(notice, screenshots=[]))
        broadcast_turn(turn.project_id, turn_payload(turn))

    transaction.on_commit(_broadcast)


def mark_stop_requested(
    turn: SetupTurn, user: CustomUser, *, reset: bool = False
) -> bool:
    """Stop a still-live turn; broadcast on commit.

    A queued turn is closed as stopped at once: the worker claims only queued
    turns, so none will run it, and a busy queue cannot keep it blocking sends
    and runs. A running turn gets ``stop_requested`` for its worker to honour.
    """
    if _close_queued_turn(turn, user, reset=reset):
        return True
    updated = SetupTurn.objects.filter(
        pk=turn.pk, status__in=LIVE_TURN_STATUSES
    ).update(stop_requested=True, stop_requested_by=user)
    if not updated:
        return False
    turn.stop_requested = True
    turn.stop_requested_by = user
    # The worker may have claimed it since it was read; broadcast its status.
    turn.refresh_from_db(fields=["status", "started_at"])
    transaction.on_commit(lambda: broadcast_turn(turn.project_id, turn_payload(turn)))
    return True


def _close_queued_turn(turn: SetupTurn, user: CustomUser, *, reset: bool) -> bool:
    outcome = stopped_outcome(conversation_archived=reset)
    now = timezone.now()
    closed = SetupTurn.objects.filter(pk=turn.pk, status=SetupTurnStatus.QUEUED).update(
        status=outcome.status,
        ended_reason=outcome.ended_reason,
        stop_requested=True,
        stop_requested_by=user,
        finished_at=now,
    )
    if not closed:
        return False
    turn.status = outcome.status
    turn.ended_reason = outcome.ended_reason
    turn.stop_requested = True
    turn.stop_requested_by = user
    turn.finished_at = now
    notice = SetupMessage.objects.create(
        conversation_id=turn.conversation_id,
        turn=turn,
        kind=SetupMessageKind.NOTICE,
        text=outcome.notice,
    )

    def _broadcast() -> None:
        broadcast_message(turn.project_id, message_payload(notice, screenshots=[]))
        broadcast_turn(turn.project_id, turn_payload(turn))

    transaction.on_commit(_broadcast)
    return True
