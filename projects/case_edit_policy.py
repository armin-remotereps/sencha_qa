"""Who may edit a finished run case's result, and which cases (pure, no query).

An admin (an active superuser, ``has_superuser_project_control``) corrects a
Passed, Failed or Blocked case by hand (``projects/case_edit_write.py``). The
texts here are user-safe and shown as flashes or on the editor page.
"""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser

from accounts.models import CustomUser
from projects.models import CaseEditKind, TestRunTestCase, TestRunTestCaseStatus
from projects.project_access import has_superuser_project_control

EDITABLE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
)

# Edit kinds that change the case's result and keep a ``before_state``.
REVERTABLE_KINDS: frozenset[str] = frozenset({CaseEditKind.RESULT, CaseEditKind.REVERT})

# ``CaseResultEdit.changes`` field names of the result edit and the revert
# (steps use ``case_edit_checkpoint.FIELD_STEP_*``).
FIELD_STATUS = "status"
FIELD_RESULT = "result"
FIELD_STEPS = "steps"

CASE_CHANGED = "The case changed since you opened the editor. Reload the page."
SEND_LIVE = "A TestRail send of this run is in progress. Edit the case after it."
UPLOADING = (
    "This case's screenshots are still uploading from the Test Runner. Edit the "
    "case when the upload has finished."
)
NOT_FINISHED = "Only a Passed, Failed or Blocked case can be edited."
NEEDS_RETEST = "Undo the re-test mark or re-run the case first."
MANUAL_CLAIMED = (
    "A tester is performing this case. Edit it after it is handed in or released."
)


def can_admin_edit(user: CustomUser | AnonymousUser) -> bool:
    """Whether the user may edit run case results (an active superuser)."""
    return isinstance(user, CustomUser) and has_superuser_project_control(user)


def edit_blocker(pivot: TestRunTestCase) -> str | None:
    """Why the case cannot be edited now; None when it can (no query)."""
    if pivot.manual_claim_token:
        return MANUAL_CLAIMED
    if pivot.status == TestRunTestCaseStatus.NEEDS_RETEST:
        return NEEDS_RETEST
    if pivot.status not in EDITABLE_STATUSES:
        return NOT_FINISHED
    return None


def is_case_editable(pivot: TestRunTestCase) -> bool:
    """Whether the case's status allows an admin edit (no query)."""
    return edit_blocker(pivot) is None


def is_revertable(kind: str, reverted_by_id: int | None) -> bool:
    """Whether the case's newest edit (of any kind) may be reverted (no query).

    Only a result edit or a revert that is not reverted itself. Any later
    edit (screenshot edits included) makes an earlier one final. The revert
    service also checks the edit's ``before_state`` under the case lock.
    """
    return kind in REVERTABLE_KINDS and reverted_by_id is None
