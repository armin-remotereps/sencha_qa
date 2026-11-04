"""Which run cases the analysis classifies, and which it only counts (pure)."""

from __future__ import annotations

from enum import StrEnum

from projects.models import TestRunTestCaseStatus


class CaseScope(StrEnum):
    # Classified: Failed, Blocked, Needs re-test, Cancelled with a result.
    ANALYSED = "analysed"
    # Counted only.
    PASSED = "passed"
    NOT_FINISHED = "not_finished"


ANALYSED_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
    TestRunTestCaseStatus.NEEDS_RETEST,
)


def case_scope(status: str, result: str) -> CaseScope:
    if status in ANALYSED_STATUSES:
        return CaseScope.ANALYSED
    if status == TestRunTestCaseStatus.CANCELLED and result.strip():
        return CaseScope.ANALYSED
    if status == TestRunTestCaseStatus.SUCCESS:
        return CaseScope.PASSED
    return CaseScope.NOT_FINISHED


def is_untested(status: str) -> bool:
    """A Cancelled case with a result: shown as "Untested" in its category."""
    return status == TestRunTestCaseStatus.CANCELLED
