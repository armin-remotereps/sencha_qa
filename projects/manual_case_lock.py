"""Lock a Manual run case for a claim, release, hand-in or pause.

Shared by the manual services so every one of them takes the same locks in
the same order as the rest of ``projects``: project, then run
(``run_reopen_services.lock_project_and_run``), then the case. The case is
always resolved inside the caller's project, so a Test Runner can only reach
its own project's cases.
"""

from __future__ import annotations

import secrets

from projects.exceptions import ManualRunError
from projects.manual_run_policy import is_manual_run
from projects.models import TestRun, TestRunTestCase
from projects.run_reopen_services import lock_project_and_run

CASE_GONE = "This case is no longer part of a manual run of this project."
NOT_MANUAL = "This run is not a manual run."


def lock_manual_run_case(
    *, project_id: int, pivot_id: int, with_test_case: bool = False
) -> tuple[TestRun, TestRunTestCase]:
    """Lock project, run and case; the locked run and case (logs deferred).

    Must be called inside ``transaction.atomic()``. ``with_test_case`` joins
    the case's test case (the claim builds the steps from it). Raises
    ``ManualRunError`` when the case is not one of ``project_id``'s or its run
    is not in Manual mode; the run and case states are the caller's checks.
    """
    test_run_id = (
        TestRunTestCase.objects.filter(pk=pivot_id, test_run__project_id=project_id)
        .values_list("test_run_id", flat=True)
        .first()
    )
    if test_run_id is None:
        raise ManualRunError(CASE_GONE)
    _project, test_run = lock_project_and_run(test_run_id)
    if not is_manual_run(test_run):
        raise ManualRunError(NOT_MANUAL)
    cases = TestRunTestCase.objects.select_for_update(of=("self",)).defer("logs")
    if with_test_case:
        cases = cases.select_related("test_case")
    pivot = cases.filter(pk=pivot_id, test_run_id=test_run.pk).first()
    if pivot is None:
        raise ManualRunError(CASE_GONE)
    return test_run, pivot


def claim_matches(pivot: TestRunTestCase, claim_token: str) -> bool:
    """The case holds the claim ``claim_token`` (constant-time compare)."""
    return bool(pivot.manual_claim_token) and secrets.compare_digest(
        pivot.manual_claim_token, claim_token
    )
