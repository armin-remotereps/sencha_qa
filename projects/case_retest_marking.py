"""Write and clear a case's Needs re-test mark (in memory, on a locked case).

Shared by the bulk marking use case (`run_case_retest_services`) and the help
and person step resolutions, so every path writes the mark the same way. Must
not import `projects.services` callers' modules (help, person step, retest
services): they import this one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from accounts.models import CustomUser
from projects.models import (
    RETEST_FIELDS,
    RETEST_VERDICT_STATUSES,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_reopen_services import CHECKED_SENT_FIELDS, clear_checked_and_sent

# Every field `apply_retest_mark` writes, for ``save`` / ``bulk_update``.
RETEST_MARK_FIELDS: Final[tuple[str, ...]] = (
    "status",
    "finished_at",
    *RETEST_FIELDS,
    *CHECKED_SENT_FIELDS,
    "updated_at",
)


def apply_retest_mark(
    pivot: TestRunTestCase, *, user: CustomUser, note: str, now: datetime
) -> None:
    """Mark the locked case Needs re-test in memory (`RETEST_MARK_FIELDS`).

    The verdict text, log, screenshots, checkpoint, ``blocked_reason`` and
    ``failed_by`` are kept, so Undo can restore a verdict as it was. A case with
    no verdict yet gets ``finished_at = now``. Checked/Sent marks are cleared:
    the TestRail result to send is now Retest.
    """
    pivot.retest_from_status = pivot.status
    pivot.retest_marked_at = now
    pivot.retest_marked_by = user
    pivot.retest_note = note
    if pivot.status not in RETEST_VERDICT_STATUSES or pivot.finished_at is None:
        pivot.finished_at = now
    pivot.status = TestRunTestCaseStatus.NEEDS_RETEST
    clear_checked_and_sent(pivot)
    # bulk_update does not run auto_now.
    pivot.updated_at = now


def clear_retest_mark(pivot: TestRunTestCase) -> None:
    """Clear the mark's fields in memory (`RETEST_FIELDS`); the status is the caller's."""
    pivot.retest_from_status = ""
    pivot.retest_marked_at = None
    pivot.retest_marked_by = None
    pivot.retest_note = ""
