"""An admin edits a finished case's status, result summary and plan steps.

One use case, one save and one ``CaseResultEdit`` row (kind ``result``),
through the shared write path in ``projects/case_edit_write.py``. No AI, no
test-session recording, no credit change.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.db import transaction

from accounts.models import CustomUser
from projects.case_edit_checkpoint import FieldChange, StepEdit, apply_step_edits
from projects.case_edit_checkpoint import field_change as make_field_change
from projects.case_edit_policy import EDITABLE_STATUSES, FIELD_RESULT, FIELD_STATUS
from projects.case_edit_write import (
    finish_edit,
    lock_editable_case,
    result_before_state,
)
from projects.case_result_text import result_status_label
from projects.exceptions import CaseEditError
from projects.models import (
    BlockedReason,
    CaseEditKind,
    CaseResultEdit,
    FailedBy,
    TestRunTestCaseStatus,
)

_NOTHING_CHANGED = "Nothing changed."
_CHOOSE_STATUS = "Choose Passed, Failed or Blocked."


def edit_case_result(
    *,
    pivot_id: int,
    user: CustomUser,
    base_key: str,
    status: str,
    result: str,
    steps: Sequence[StepEdit],
) -> CaseResultEdit:
    """Save the admin's status, summary and step edits together.

    A new status gets the reason Apply writes (Blocked: person review;
    Failed: review; Passed: neither); a case keeping its status keeps its
    reason. Raises ``CaseEditForbiddenError`` for a non-admin and
    ``CaseEditError`` (user-safe) otherwise, also when nothing changed.
    """
    with transaction.atomic():
        locked = lock_editable_case(pivot_id=pivot_id, user=user, base_key=base_key)
        pivot = locked.pivot
        if status not in EDITABLE_STATUSES:
            raise CaseEditError(_CHOOSE_STATUS)
        before_state = result_before_state(pivot)
        checkpoint_json, step_changes = apply_step_edits(
            pivot.execution_checkpoint, steps
        )

        changes: list[FieldChange] = []
        update_fields: list[str] = []
        if status != pivot.status:
            changes.append(
                make_field_change(
                    FIELD_STATUS,
                    None,
                    result_status_label(pivot.status),
                    result_status_label(status),
                )
            )
            new_status: str = status
            pivot.status = new_status
            pivot.blocked_reason = (
                BlockedReason.PERSON_REVIEW
                if new_status == TestRunTestCaseStatus.BLOCKED
                else ""
            )
            if new_status == TestRunTestCaseStatus.FAILED:
                pivot.failed_by = FailedBy.REVIEW
            elif new_status == TestRunTestCaseStatus.SUCCESS:
                pivot.failed_by = ""
            update_fields.extend(["status", "blocked_reason", "failed_by"])
        if result != pivot.result:
            changes.append(make_field_change(FIELD_RESULT, None, pivot.result, result))
            pivot.result = result
            update_fields.append("result")
        if step_changes:
            changes.extend(step_changes)
            pivot.execution_checkpoint = checkpoint_json
            update_fields.append("execution_checkpoint")
        if not changes:
            raise CaseEditError(_NOTHING_CHANGED)

        return finish_edit(
            locked=locked,
            user=user,
            kind=CaseEditKind.RESULT,
            changes=[change.to_json() for change in changes],
            before_state=before_state,
            update_fields=update_fields,
        )
