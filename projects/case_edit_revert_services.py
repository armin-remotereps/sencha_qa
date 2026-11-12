"""Revert the newest admin result edit of a finished run case.

Only the case's newest edit may be reverted, and only when it is a result edit
or a revert (``case_edit_policy.is_revertable``) and the case still holds the
state that edit left (its ``after_fingerprint``). The revert restores the
edit's ``before_state`` (the result and the admin marker as they were; a
marker key missing from an older row restores a blank marker) and writes a ``revert`` audit row with its own
``before_state``, so a revert can itself be reverted. Screenshot edits are
undone by acting again; a delete cannot be undone.
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction

from accounts.models import CustomUser
from projects.case_edit_checkpoint import FieldChange, field_change
from projects.case_edit_policy import (
    CASE_CHANGED,
    EDITABLE_STATUSES,
    FIELD_RESULT,
    FIELD_STATUS,
    FIELD_STEPS,
    can_admin_edit,
    is_revertable,
)
from projects.case_edit_write import (
    ADMIN_MARKER_FIELDS,
    finish_edit,
    lock_editable_case,
    result_before_state,
)
from projects.exceptions import CaseEditError, CaseEditForbiddenError
from projects.models import CaseEditKind, CaseResultEdit

_EDIT_GONE = "This edit no longer exists."
_NOT_NEWEST = "Only the newest edit of the case can be reverted."
_NOT_REVERTABLE = (
    "This edit cannot be reverted: only a change of the result or steps "
    "(or a revert of one) can."
)
_OUTDATED = (
    "This edit can no longer be reverted: the case changed after it. Reload the page."
)
_BAD_SNAPSHOT = "This edit has no usable earlier state to restore."

_RESTORED_FIELDS: list[str] = [
    "status",
    "blocked_reason",
    "failed_by",
    "result",
    "execution_checkpoint",
    *ADMIN_MARKER_FIELDS,
]
_STEPS_BEFORE = "Steps as edited"
_STEPS_AFTER = "Steps as they were before the edit"


def revert_case_edit(
    *, pivot_id: int, edit_id: int, user: CustomUser
) -> CaseResultEdit:
    """Revert edit ``edit_id`` of case ``pivot_id``; returns the revert row.

    Raises ``CaseEditForbiddenError`` for a non-admin and ``CaseEditError``
    (user-safe) for a missing, older, non-revertable or outdated edit and for
    every refusal of ``lock_editable_case``.
    """
    if not can_admin_edit(user):
        raise CaseEditForbiddenError()
    with transaction.atomic():
        edit = (
            CaseResultEdit.objects.filter(pk=edit_id, test_run_test_case_id=pivot_id)
            .only("id", "kind", "before_state", "after_fingerprint", "reverted_by_id")
            .first()
        )
        if edit is None:
            raise CaseEditError(_EDIT_GONE)
        try:
            locked = lock_editable_case(
                pivot_id=pivot_id, user=user, base_key=edit.after_fingerprint
            )
        except CaseEditError as exc:
            if exc.message == CASE_CHANGED:
                raise CaseEditError(_OUTDATED) from exc
            raise
        pivot = locked.pivot
        # Read again under the case lock: every edit writer holds it.
        newest = (
            CaseResultEdit.objects.filter(test_run_test_case_id=pivot.pk)
            .only("id", "kind", "reverted_by_id")
            .order_by("-edited_at", "-id")
            .first()
        )
        if newest is None:
            raise CaseEditError(_EDIT_GONE)
        if newest.pk != edit.pk:
            raise CaseEditError(_NOT_NEWEST)
        if not is_revertable(newest.kind, newest.reverted_by_id):
            raise CaseEditError(_NOT_REVERTABLE)

        snapshot = _snapshot(edit.before_state)
        before_state = result_before_state(pivot)
        changes = _diff(before_state, snapshot)
        pivot.status = _str(snapshot, "status")
        pivot.blocked_reason = _str(snapshot, "blocked_reason")
        pivot.failed_by = _str(snapshot, "failed_by")
        pivot.result = _str(snapshot, "result")
        pivot.execution_checkpoint = snapshot["execution_checkpoint"]
        pivot.admin_edited_at = _marker_at(snapshot.get("admin_edited_at"))
        pivot.admin_edited_label = _marker_text(snapshot.get("admin_edited_label"))
        pivot.admin_edit_fingerprint = _marker_text(
            snapshot.get("admin_edit_fingerprint")
        )
        return finish_edit(
            locked=locked,
            user=user,
            kind=CaseEditKind.REVERT,
            changes=[change.to_json() for change in changes],
            before_state=before_state,
            update_fields=_RESTORED_FIELDS,
            reverts=edit,
        )


def _snapshot(before_state: object) -> dict[str, object]:
    """The edit's ``before_state``, checked to be a restorable finished result."""
    if not isinstance(before_state, dict):
        raise CaseEditError(_BAD_SNAPSHOT)
    for key in ("status", "blocked_reason", "failed_by", "result"):
        if not isinstance(before_state.get(key), str):
            raise CaseEditError(_BAD_SNAPSHOT)
    if before_state["status"] not in EDITABLE_STATUSES:
        raise CaseEditError(_BAD_SNAPSHOT)
    if not isinstance(before_state.get("execution_checkpoint"), dict):
        raise CaseEditError(_BAD_SNAPSHOT)
    return before_state


def _marker_at(value: object) -> datetime | None:
    """The restored ``admin_edited_at``; None when missing or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _marker_text(value: object) -> str:
    """A restored marker text; blank when missing (rows from before markers)."""
    return value if isinstance(value, str) else ""


def _str(snapshot: dict[str, object], key: str) -> str:
    value = snapshot[key]
    if not isinstance(value, str):
        raise CaseEditError(_BAD_SNAPSHOT)
    return value


def _diff(current: dict[str, object], restored: dict[str, object]) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for field in (FIELD_STATUS, FIELD_RESULT):
        before = _str(current, field)
        after = _str(restored, field)
        if before != after:
            changes.append(field_change(field, None, before, after))
    if current["execution_checkpoint"] != restored["execution_checkpoint"]:
        changes.append(field_change(FIELD_STEPS, None, _STEPS_BEFORE, _STEPS_AFTER))
    return changes
