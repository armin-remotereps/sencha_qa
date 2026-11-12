"""Use cases: an admin sets a case screenshot's TestRail attach setting, or
deletes the screenshot (``projects/case_edit_write.py`` for the shared path).

Setting a shot back to "Decided by the review" also forgets the case's stored
evidence review: a case whose candidates all carried an override was marked
reviewed without an AI call, so the shot would otherwise never be judged.

A delete removes the row now and its file once the transaction commits; a
file that cannot be removed is logged, never raised. Copies already attached
in TestRail stay there.
"""

from __future__ import annotations

import logging

from django.core.files.storage import Storage
from django.db import transaction

from accounts.models import CustomUser
from projects.case_edit_checkpoint import field_change
from projects.case_edit_write import finish_edit, lock_editable_case, screenshot_label
from projects.exceptions import CaseEditError
from projects.models import (
    UPLOADED_SCREENSHOT_TOOL_NAME,
    CaseEditKind,
    CaseResultEdit,
    EvidenceOverride,
    EvidenceReviewState,
    TestRunScreenshot,
)
from projects.testrail_evidence import effective_override

logger = logging.getLogger(__name__)

FIELD_SCREENSHOT_ATTACH = "screenshot_attach"
FIELD_SCREENSHOT_DELETED = "screenshot_deleted"

_NOT_ON_CASE = "This screenshot is not part of this case."
_UNCHANGED = "Nothing changed."
_BAD_OVERRIDE = "Choose how this screenshot is attached to TestRail."

# The case's review fields cleared when a shot goes back to the AI review.
_REVIEW_RESET_FIELDS: tuple[str, ...] = (
    "evidence_review_state",
    "evidence_review_key",
    "evidence_review_error",
)


def _lock_case_shot(*, pivot_id: int, shot_id: int) -> TestRunScreenshot:
    """Lock one screenshot of the case (after ``lock_editable_case``)."""
    shot = (
        TestRunScreenshot.objects.select_for_update()
        .filter(pk=shot_id, test_run_test_case_id=pivot_id)
        .first()
    )
    if shot is None:
        raise CaseEditError(_NOT_ON_CASE)
    return shot


def set_screenshot_evidence(
    *, pivot_id: int, shot_id: int, user: CustomUser, base_key: str, override: str
) -> CaseResultEdit:
    """Set one screenshot's TestRail attach setting (auto / include / exclude)."""
    if override not in EvidenceOverride.values:
        raise CaseEditError(_BAD_OVERRIDE)
    with transaction.atomic():
        locked = lock_editable_case(pivot_id=pivot_id, user=user, base_key=base_key)
        pivot = locked.pivot
        shot = _lock_case_shot(pivot_id=pivot_id, shot_id=shot_id)
        # An override set on an earlier verdict no longer applies: compare
        # with what applies now, and scope the new one to this verdict.
        before = effective_override(
            shot.evidence_override,
            shot.evidence_override_finished_at,
            pivot.finished_at,
        )
        if before == override:
            raise CaseEditError(_UNCHANGED)
        shot.evidence_override = override
        shot.evidence_override_finished_at = pivot.finished_at
        shot.save(
            update_fields=["evidence_override", "evidence_override_finished_at"]
        )
        locked.shot_keys[shot.pk] = override
        update_fields: list[str] = []
        if override == EvidenceOverride.AUTO:
            pivot.evidence_review_state = EvidenceReviewState.NONE
            pivot.evidence_review_key = ""
            pivot.evidence_review_error = ""
            update_fields.extend(_REVIEW_RESET_FIELDS)
        change = field_change(
            FIELD_SCREENSHOT_ATTACH,
            shot.step_num,
            EvidenceOverride(before).label,
            EvidenceOverride(override).label,
        )
        return finish_edit(
            locked=locked,
            user=user,
            kind=CaseEditKind.SCREENSHOT_EVIDENCE,
            changes=[change.to_json()],
            before_state=None,
            update_fields=update_fields,
            screenshot=shot,
            screenshot_label=screenshot_label(shot),
        )


def delete_case_screenshot(
    *, pivot_id: int, shot_id: int, user: CustomUser, base_key: str
) -> CaseResultEdit:
    """Delete one screenshot of the case for good (its file on commit)."""
    with transaction.atomic():
        locked = lock_editable_case(pivot_id=pivot_id, user=user, base_key=base_key)
        shot = _lock_case_shot(pivot_id=pivot_id, shot_id=shot_id)
        label = screenshot_label(shot)
        step_num = shot.step_num
        file_name = shot.image.name or ""
        storage = shot.image.storage
        uploaded = shot.tool_name == UPLOADED_SCREENSHOT_TOOL_NAME
        shot.delete()
        locked.shot_keys.pop(shot_id, None)
        if uploaded:
            locked.upload_count -= 1
        if file_name:
            transaction.on_commit(lambda: _delete_file(storage, file_name))
        change = field_change(FIELD_SCREENSHOT_DELETED, step_num, label, "")
        return finish_edit(
            locked=locked,
            user=user,
            kind=CaseEditKind.SCREENSHOT_DELETE,
            changes=[change.to_json()],
            before_state=None,
            update_fields=[],
            screenshot=None,
            screenshot_label=label,
        )


def _delete_file(storage: Storage, name: str) -> None:
    """Remove a deleted screenshot's file; a failure is logged only."""
    try:
        storage.delete(name)
    except OSError:
        logger.exception("Could not delete admin-deleted screenshot %s", name)
