"""Use case: an admin uploads a screenshot to a finished case
(``projects/case_edit_write.py`` for the shared path).

The image is checked like a Manual run screenshot (``load_manual_image``: PNG
or JPEG, ``MANUAL_SHOT_MAX_BYTES``, ``MANUAL_SHOT_MAX_PIXELS``) and written
to storage before any lock (removed again if the edit is refused). It is
stored under the case's screenshot path with a random name (never the
uploader's file name), optionally tied to a plan step (and that step's
final attempt), and always attached to TestRail (``EvidenceOverride.INCLUDE``).
At most ``CASE_EDIT_MAX_UPLOADS_PER_CASE`` uploads per case.
"""

from __future__ import annotations

import secrets
from typing import Final

from django.core.files.base import ContentFile
from django.db import transaction

from accounts.models import CustomUser
from controller_client.protocol import ManualShotMime
from projects.case_edit_checkpoint import field_change, step_rows
from projects.case_edit_policy import can_admin_edit
from projects.case_edit_write import finish_edit, lock_editable_case, screenshot_label
from projects.exceptions import CaseEditError, CaseEditForbiddenError
from projects.manual_screenshot_services import load_manual_image
from projects.models import (
    CASE_EDIT_MAX_UPLOADS_PER_CASE,
    UPLOADED_SCREENSHOT_TOOL_NAME,
    CaseEditKind,
    CaseResultEdit,
    EvidenceOverride,
    TestRunScreenshot,
)

FIELD_SCREENSHOT_UPLOADED = "screenshot_uploaded"

_NOT_AN_IMAGE = "This screenshot is not a valid PNG or JPEG image."
_TOO_MANY_UPLOADS = (
    f"This case already has {CASE_EDIT_MAX_UPLOADS_PER_CASE} uploaded "
    "screenshots. Delete one first."
)
_UNKNOWN_STEP = "That step is not part of this case's plan. Reload the page."

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE: Final = b"\xff\xd8\xff"
_EXTENSIONS: Final[dict[ManualShotMime, str]] = {
    ManualShotMime.PNG: "png",
    ManualShotMime.JPEG: "jpg",
}


def _detect_mime(data: bytes) -> ManualShotMime | None:
    """The image type from its leading bytes; None when neither PNG nor JPEG."""
    if data.startswith(_PNG_SIGNATURE):
        return ManualShotMime.PNG
    if data.startswith(_JPEG_SIGNATURE):
        return ManualShotMime.JPEG
    return None


def _checked_mime(data: bytes) -> ManualShotMime:
    """The image's type once ``load_manual_image`` accepts it, else refuse."""
    mime = _detect_mime(data)
    if mime is None:
        raise CaseEditError(_NOT_AN_IMAGE)
    reason, image = load_manual_image(mime, data)
    if image is not None:
        image.close()
    if reason:
        raise CaseEditError(reason)
    return mime


def upload_case_screenshot(
    *,
    pivot_id: int,
    user: CustomUser,
    base_key: str,
    data: bytes,
    step_num: int | None,
) -> CaseResultEdit:
    """Store an uploaded screenshot on the case, optionally tied to a step.

    The file is written before any lock is taken and removed again when the
    edit is refused or fails, so storage I/O never runs under the locks.
    """
    if not can_admin_edit(user):
        # Before the image work; ``lock_editable_case`` checks it again.
        raise CaseEditForbiddenError()
    mime = _checked_mime(data)
    shot = TestRunScreenshot(
        test_run_test_case_id=pivot_id,
        tool_name=UPLOADED_SCREENSHOT_TOOL_NAME,
        step_num=step_num,
        evidence_override=EvidenceOverride.INCLUDE,
    )
    shot.image.save(
        f"uploaded_{secrets.token_hex(16)}.{_EXTENSIONS[mime]}",
        ContentFile(data),
        save=False,
    )
    try:
        with transaction.atomic():
            return _store_upload(
                shot, pivot_id=pivot_id, user=user, base_key=base_key
            )
    except Exception:
        # The row (if any) is rolled back with the transaction; the file is not.
        shot.image.delete(save=False)
        raise


def _store_upload(
    shot: TestRunScreenshot, *, pivot_id: int, user: CustomUser, base_key: str
) -> CaseResultEdit:
    """Lock the case, check the upload and save ``shot`` (file already stored)."""
    locked = lock_editable_case(pivot_id=pivot_id, user=user, base_key=base_key)
    # The include applies to the case's current verdict only.
    shot.evidence_override_finished_at = locked.pivot.finished_at
    if locked.upload_count >= CASE_EDIT_MAX_UPLOADS_PER_CASE:
        raise CaseEditError(_TOO_MANY_UPLOADS)
    if shot.step_num is not None:
        row = next(
            (
                row
                for row in step_rows(locked.pivot.execution_checkpoint)
                if row.step_num == shot.step_num
            ),
            None,
        )
        if row is None:
            raise CaseEditError(_UNKNOWN_STEP)
        shot.attempt_id = row.final_attempt_id
    shot.save()
    locked.shot_keys[shot.pk] = shot.evidence_override
    locked.upload_count += 1
    label = screenshot_label(shot)
    change = field_change(FIELD_SCREENSHOT_UPLOADED, shot.step_num, "", label)
    return finish_edit(
        locked=locked,
        user=user,
        kind=CaseEditKind.SCREENSHOT_UPLOAD,
        changes=[change.to_json()],
        before_state=None,
        update_fields=[],
        screenshot=shot,
        screenshot_label=label,
    )
