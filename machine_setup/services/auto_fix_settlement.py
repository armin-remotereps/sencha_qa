"""Hand the end of an auto-fix setup turn back to the paused run.

Its own module (re-exported by ``case_auto_fix``) because ``live_turn`` calls
it when it closes a turn, and ``case_auto_fix`` imports ``live_turn``.
"""

from __future__ import annotations

import logging

from django.db import transaction

from machine_setup.models import (
    SetupConversation,
    SetupFixResult,
    SetupTurn,
    SetupTurnEndReason,
)
from projects.case_help_services import finish_case_auto_fix
from projects.models import HELP_NOTE_MAX_LENGTH

logger = logging.getLogger(__name__)


def settle_case_auto_fix(turn: SetupTurn) -> None:
    """Report an ended auto-fix turn to its help request; never raises.

    Call it only once the turn's end state is written (so no live turn is left
    for the run's gates to see), and only by whoever wrote it, so a turn is
    settled once. Fixed means the agent's last ``report_case_fix`` said fixed
    and the turn ended with an answer. No report, a not-fixed report, Stop, a
    limit, a lost worker or a failure all count as not fixed.
    """
    if not turn.auto_fix:
        return
    try:
        # A savepoint: callers may hold an outer transaction (the project lock
        # of a send, stop or reset), which an error here must not break.
        with transaction.atomic():
            _settle(turn)
    except Exception:
        logger.exception("Setup turn %s: settling the auto-fix failed", turn.pk)


def _settle(turn: SetupTurn) -> None:
    row = (
        SetupConversation.objects.filter(pk=turn.conversation_id)
        .values("help_request_id", "fix_result", "fix_summary")
        .first()
    )
    if row is None or row["help_request_id"] is None:
        return
    fixed, summary = _fix_outcome(
        turn,
        fix_result=str(row["fix_result"]),
        fix_summary=str(row["fix_summary"]).strip(),
    )
    logger.info(
        "Setup turn %s settles help request %s: fixed=%s",
        turn.pk,
        row["help_request_id"],
        fixed,
    )
    finish_case_auto_fix(
        help_request_id=int(row["help_request_id"]),
        fixed=fixed,
        summary=summary[:HELP_NOTE_MAX_LENGTH],
    )


def _fix_outcome(
    turn: SetupTurn, *, fix_result: str, fix_summary: str
) -> tuple[bool, str]:
    answered = turn.ended_reason == SetupTurnEndReason.ANSWERED
    if fix_result == SetupFixResult.FIXED and answered:
        return True, fix_summary or "The setup agent reported the machine fixed."
    if fix_result == SetupFixResult.NOT_FIXED:
        return False, fix_summary or "The setup agent could not fix the machine."
    ended = _ended_text(turn)
    if fix_result == SetupFixResult.FIXED:
        return False, (
            f"The auto-fix turn ended ({ended}) after reporting a fix, so the fix "
            f"is not confirmed. The agent's report: {fix_summary}"
        )
    return False, f"The setup agent ended ({ended}) without reporting a fix."


def _ended_text(turn: SetupTurn) -> str:
    if turn.ended_reason:
        return SetupTurnEndReason(turn.ended_reason).label.lower()
    return str(turn.status)
