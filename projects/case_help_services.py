"""Use cases that resolve a Need help case: a person's actions and the auto-fix end.

Imports ``run_reopen_services``, ``services`` and the leaf
``machine_setup.auto_fix_stop`` / ``machine_setup.turn_state`` modules, never
``machine_setup.services`` (that package imports this one).
"""

from __future__ import annotations


def finish_case_auto_fix(*, help_request_id: int, fixed: bool, summary: str) -> None:
    """Settle a help request once its auto-fix setup turn has ended.

    Called by ``machine_setup`` after the turn's status is final. Locks project
    then run and does nothing unless the run is PAUSED. Fixed: the request is
    resolved ``auto_fixed`` and the pending cases are queued (the helped case
    first). Not fixed, or stopped: the request stays open for a person and the
    project members are notified. If a person already resolved the request
    during the auto-fix, the pending cases are queued.
    """
    raise NotImplementedError
