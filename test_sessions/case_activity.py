"""What a tester did to a run case while a test session was open.

A leaf module (no ``projects`` imports), so ``projects`` can import it at the
hook sites without an import cycle.
"""

from __future__ import annotations

from django.db import models


class CaseActivity(models.TextChoices):
    START = "start", "Started"
    RESUME = "resume", "Resumed"
    RERUN = "rerun", "Re-ran"
    REDESIGN_RERUN = "redesign_rerun", "Re-ran with a redesigned plan"
    HELP = "help", "Resolved help"
    PERSON_STEP = "person_step", "Performed a step"
    RESULT_CHANGE = "result_change", "Changed the result"
    RETEST = "retest", "Marked for re-test"


# Actions that throw away the case's current verdict: other sessions' rows for
# the case are frozen first so they keep the outcome they saw.
VERDICT_DISCARDING = frozenset(
    {
        CaseActivity.RESUME,
        CaseActivity.RERUN,
        CaseActivity.REDESIGN_RERUN,
        CaseActivity.RETEST,
    }
)
