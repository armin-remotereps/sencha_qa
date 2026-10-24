"""Ask a running auto-fix turn to stop.

A leaf module (imports only ``machine_setup.models``) so ``projects`` can stop
the auto-fix of a help request without importing the agent runtime.
"""

from __future__ import annotations

from machine_setup.models import LIVE_TURN_STATUSES, SetupTurn


def request_auto_fix_stop(help_request_id: int) -> bool:
    """Flag the live auto-fix turn of a help request to stop.

    The worker honours ``stop_requested`` at its next iteration or tool
    boundary, as for a person's Stop. A queued turn is flagged too: the worker
    checks the flag when it claims the turn. No broadcast is sent; the turn's
    own finish broadcasts its end. Returns whether a live auto-fix turn exists (flagged now or before).
    """
    updated = SetupTurn.objects.filter(
        auto_fix=True,
        conversation__help_request_id=help_request_id,
        status__in=LIVE_TURN_STATUSES,
    ).update(stop_requested=True)
    return updated > 0
