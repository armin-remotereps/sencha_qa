"""Every case analysis write runs only while its Machine setup turn runs.

Same rule as ``machine_setup.services.run_turn._finish``: a stopped,
force-stopped or lost turn writes nothing.
"""

from __future__ import annotations

from machine_setup.models import SetupTurn, SetupTurnStatus
from run_triage.exceptions import TriageTurnClosedError

TURN_CLOSED_MESSAGE = (
    "This turn was stopped or has ended, so the case analysis was not changed."
)


def require_running_turn(turn_id: int) -> None:
    """Lock the turn row; ``TriageTurnClosedError`` unless it is RUNNING.

    Call inside ``transaction.atomic()``, before the write it guards.
    """
    status = (
        SetupTurn.objects.select_for_update()
        .filter(id=turn_id)
        .values_list("status", flat=True)
        .first()
    )
    if status != SetupTurnStatus.RUNNING:
        raise TriageTurnClosedError(TURN_CLOSED_MESSAGE)
