"""The project's live turn: find it (closing it when its worker is gone) and stop it.

Shared by send, stop, reset and the auto-fix start, which all run under the
project row lock. Closing an auto-fix turn here also settles its help request
(``settle_case_auto_fix``), since its worker never will.
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.db.models import Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.events import broadcast_message, broadcast_turn
from machine_setup.models import (
    LIVE_TURN_STATUSES,
    SetupMessage,
    SetupMessageKind,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.payloads import message_payload, turn_payload
from machine_setup.services.activity_recorder import (
    TOOL_INTERRUPTED_TEXT,
    fail_running_rows,
)
from machine_setup.services.auto_fix_settlement import settle_case_auto_fix
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
    close_live_turn(
        turn,
        now,
        status=SetupTurnStatus.FAILED,
        ended_reason=SetupTurnEndReason.WORKER_LOST,
        error_message=WORKER_LOST_MESSAGE,
        notice_text=WORKER_LOST_MESSAGE,
    )


def close_live_turn(
    turn: SetupTurn,
    now: datetime,
    *,
    status: SetupTurnStatus,
    ended_reason: SetupTurnEndReason,
    error_message: str,
    notice_text: str,
    require_status: tuple[SetupTurnStatus, ...] = LIVE_TURN_STATUSES,
) -> bool:
    """End a turn no worker will end (lost, or force stopped); broadcast on commit.

    Conditional on the turn still having one of ``require_status``. Fails its
    running tool rows (nested sub-task rows too; a delegation row becomes
    interrupted), adds a notice and settles an auto-fix turn, since its worker
    never will. Returns whether it closed the turn.
    """
    closed = SetupTurn.objects.filter(pk=turn.pk, status__in=require_status).update(
        status=status,
        ended_reason=ended_reason,
        error_message=error_message,
        finished_at=now,
    )
    if not closed:
        return False
    turn.status = status
    turn.ended_reason = ended_reason
    turn.error_message = error_message
    turn.finished_at = now
    failed_rows = fail_running_rows(turn_id=turn.pk, text=TOOL_INTERRUPTED_TEXT)
    notice = SetupMessage.objects.create(
        conversation_id=turn.conversation_id,
        turn=turn,
        kind=SetupMessageKind.NOTICE,
        text=notice_text,
    )

    def _broadcast() -> None:
        for row in failed_rows:
            broadcast_message(turn.project_id, message_payload(row))
        broadcast_message(turn.project_id, message_payload(notice, screenshots=[]))
        broadcast_turn(turn.project_id, turn_payload(turn))

    transaction.on_commit(_broadcast)
    settle_case_auto_fix(turn)
    return True


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
    # A second Stop keeps the first press time, so it never delays Force stop.
    updated = SetupTurn.objects.filter(
        pk=turn.pk, status__in=LIVE_TURN_STATUSES
    ).update(
        stop_requested=True,
        stop_requested_by=user,
        stop_requested_at=Coalesce("stop_requested_at", Value(timezone.now())),
    )
    if not updated:
        return False
    # The worker may have claimed (or even finished) it since it was read;
    # broadcast the row as it is now, stop flag included.
    turn.refresh_from_db()
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
        stop_requested_at=now,
        finished_at=now,
    )
    if not closed:
        return False
    turn.status = outcome.status
    turn.ended_reason = outcome.ended_reason
    turn.stop_requested = True
    turn.stop_requested_by = user
    turn.stop_requested_at = now
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
    # The worker never claims a closed queued turn, so it never settles it.
    settle_case_auto_fix(turn)
    return True
