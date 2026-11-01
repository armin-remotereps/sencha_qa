"""Finish a Manual run once none of its cases is left to perform.

A Manual run has no execution chain, so no worker finishes it: every path that
settles or removes its last open case calls ``finish_manual_run_if_settled``
(the hand-in, and a Needs re-test mark of the claimed case). It goes through
``services.finish_test_run``, conditional on STARTED and the run's generation,
so exactly one caller finishes the run.
"""

from __future__ import annotations

from django.db import transaction

from projects.models import TestRun, TestRunStatus, TestRunTestCaseStatus
from projects.services import broadcast_test_run_status, finish_test_run

# A case still to perform (or, in other modes, waiting): the run goes on.
_OPEN_CASE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.CREATED,
    TestRunTestCaseStatus.IN_PROGRESS,
    TestRunTestCaseStatus.NEED_HELP,
    TestRunTestCaseStatus.AWAITING_PERSON,
)


def finish_manual_run_if_settled(test_run: TestRun) -> bool:
    """Finish the locked, STARTED manual run when no case is open (one query).

    Call it inside the action's transaction, with the run locked and its
    ``execution_generation`` loaded. True when this call finished the run; the
    status broadcast is sent on commit.
    """
    if test_run.status != TestRunStatus.STARTED:
        return False
    if test_run.pivot_entries.filter(status__in=_OPEN_CASE_STATUSES).exists():
        return False
    if not finish_test_run(
        test_run,
        from_status=TestRunStatus.STARTED,
        generation=test_run.execution_generation,
    ):
        return False
    transaction.on_commit(lambda: broadcast_test_run_status(test_run))
    return True
