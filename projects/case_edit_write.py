"""The one write path of an admin edit of a finished run case.

Every edit use case (result and steps, screenshot evidence, delete, upload,
revert) runs inside ``transaction.atomic()``:

1. ``lock_editable_case`` locks project, then run, then case (the order every
   case writer uses) and refuses a non-admin, a case that is not editable, a
   live TestRail send, a Manual hand-in still uploading and a stale editor
   (base key). It returns a ``LockedCase`` with the case's screenshot keys;
2. the use case changes the case and/or its screenshots, and keeps
   ``LockedCase.shot_keys`` in step with its screenshot changes;
3. ``finish_edit`` clears Checked/Sent, retires pending result-chat proposals,
   sets the admin marker (result edits only; a revert restores the earlier
   one itself), writes the ``CaseResultEdit`` audit row and broadcasts on
   commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.case_edit_checkpoint import edit_base_key
from projects.case_edit_policy import (
    CASE_CHANGED,
    UPLOADING,
    can_admin_edit,
    edit_blocker,
)
from projects.case_result_change_services import supersede_pending_result_change
from projects.case_result_fingerprint import result_fingerprint
from projects.case_result_text import person_label
from projects.case_review_redesign_store import supersede_case_review_drafts
from projects.exceptions import CaseEditError, CaseEditForbiddenError
from projects.manual_current_submission import uploading_manual_case_ids
from projects.manual_run_policy import is_manual_checkpoint
from projects.models import (
    UPLOADED_SCREENSHOT_TOOL_NAME,
    CaseEditKind,
    CaseResultEdit,
    TestRun,
    TestRunScreenshot,
    TestRunTestCase,
)
from projects.run_reopen_services import (
    CHECKED_SENT_FIELDS,
    clear_checked_and_sent,
    list_testrail_push_blockers,
    lock_project_and_run,
)
from projects.services import broadcast_run_and_pivots

_CASE_GONE = "This case no longer exists."
_LABEL_MAX_LENGTH = 255

ADMIN_MARKER_FIELDS: tuple[str, ...] = (
    "admin_edited_at",
    "admin_edited_label",
    "admin_edit_fingerprint",
)


@dataclass
class LockedCase:
    """The locked run and case of an edit, with the case's screenshots.

    ``shot_keys`` maps every screenshot id of the case to its
    ``evidence_override``; ``upload_count`` is how many of them were uploaded
    by an admin. A use case that changes, deletes or adds a screenshot
    updates them in memory, so ``finish_edit`` needs no query for its
    ``after_fingerprint``.
    """

    run: TestRun
    pivot: TestRunTestCase
    shot_keys: dict[int, str]
    upload_count: int


def lock_editable_case(
    *, pivot_id: int, user: CustomUser, base_key: str
) -> LockedCase:
    """Lock project, run and case and check the edit may go ahead.

    Must run inside ``transaction.atomic()``. Raises ``CaseEditForbiddenError``
    for a non-admin and ``CaseEditError`` (user-safe) otherwise. 5 queries
    (6 for a Manual case: its hand-in's upload state).
    """
    if not can_admin_edit(user):
        raise CaseEditForbiddenError()
    ids = (
        TestRunTestCase.objects.filter(pk=pivot_id)
        .values_list("test_run_id", "test_run__project_id")
        .first()
    )
    if ids is None:
        raise CaseEditError(_CASE_GONE)
    test_run_id, project_id = ids
    _, test_run = lock_project_and_run(test_run_id, project_id=project_id)
    # Logs can be large and are never touched here.
    pivot = (
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(pk=pivot_id)
        .first()
    )
    if pivot is None:
        raise CaseEditError(_CASE_GONE)
    blocker = edit_blocker(pivot)
    if blocker is not None:
        raise CaseEditError(blocker)
    push_blockers = list_testrail_push_blockers(test_run)
    if push_blockers:
        raise CaseEditError(push_blockers[0])
    if is_manual_checkpoint(pivot.execution_checkpoint) and (
        uploading_manual_case_ids([pivot.pk])
    ):
        raise CaseEditError(UPLOADING)
    shot_keys: dict[int, str] = {}
    upload_count = 0
    for shot_id, override, tool_name in TestRunScreenshot.objects.filter(
        test_run_test_case_id=pivot.pk
    ).values_list("id", "evidence_override", "tool_name"):
        shot_keys[shot_id] = override
        if tool_name == UPLOADED_SCREENSHOT_TOOL_NAME:
            upload_count += 1
    if edit_base_key(pivot, list(shot_keys.items())) != base_key:
        raise CaseEditError(CASE_CHANGED)
    return LockedCase(
        run=test_run, pivot=pivot, shot_keys=shot_keys, upload_count=upload_count
    )


def result_before_state(pivot: TestRunTestCase) -> dict[str, object]:
    """The case's result and admin marker as they are, for ``before_state``."""
    return {
        "status": pivot.status,
        "blocked_reason": pivot.blocked_reason,
        "failed_by": pivot.failed_by,
        "result": pivot.result,
        "execution_checkpoint": pivot.execution_checkpoint,
        "admin_edited_at": (
            pivot.admin_edited_at.isoformat() if pivot.admin_edited_at else None
        ),
        "admin_edited_label": pivot.admin_edited_label,
        "admin_edit_fingerprint": pivot.admin_edit_fingerprint,
    }


def screenshot_label(shot: TestRunScreenshot) -> str:
    """A short description that keeps a deleted screenshot readable in history.

    E.g. "step 4, take_screenshot, 2026-11-12 10:03 UTC".
    """
    parts: list[str] = []
    if shot.step_num is not None:
        parts.append(f"step {shot.step_num}")
    parts.append(shot.tool_name)
    if shot.created_at is not None:
        parts.append(shot.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"))
    return ", ".join(parts)[:_LABEL_MAX_LENGTH]


def finish_edit(
    *,
    locked: LockedCase,
    user: CustomUser,
    kind: str,
    changes: list[dict[str, object]],
    before_state: dict[str, object] | None,
    update_fields: list[str],
    screenshot: TestRunScreenshot | None = None,
    screenshot_label: str = "",
    reverts: CaseResultEdit | None = None,
) -> CaseResultEdit:
    """Save the edited case and write its audit row (after ``lock_editable_case``).

    ``locked.pivot`` already holds the new values of ``update_fields`` (may be
    empty, e.g. for screenshot edits) and ``locked.shot_keys`` the case's
    screenshots after the edit. Clears Checked/Sent so a new TestRail result
    can be sent, retires the case's pending result-chat proposals and, on
    commit, broadcasts the run and the case. A result edit sets the admin
    marker; other kinds leave it (a revert passes the restored marker in
    ``update_fields``). With ``reverts``, that edit's ``reverted_by`` is set
    to the new row.
    """
    pivot = locked.pivot
    run = locked.run
    label = person_label(user)[:_LABEL_MAX_LENGTH]
    clear_checked_and_sent(pivot)
    supersede_pending_result_change(pivot.pk)
    supersede_case_review_drafts(pivot.pk)
    marker_fields: tuple[str, ...] = ()
    if kind == CaseEditKind.RESULT:
        pivot.admin_edited_at = timezone.now()
        pivot.admin_edited_label = label
        pivot.admin_edit_fingerprint = result_fingerprint(pivot)
        marker_fields = ADMIN_MARKER_FIELDS
    pivot.save(
        update_fields=list(
            dict.fromkeys(
                [
                    *update_fields,
                    *CHECKED_SENT_FIELDS,
                    *marker_fields,
                    "updated_at",
                ]
            )
        )
    )
    edit = CaseResultEdit.objects.create(
        test_run_test_case=pivot,
        kind=kind,
        edited_by=user,
        edited_by_label=label,
        changes=changes,
        before_state=before_state,
        after_fingerprint=edit_base_key(pivot, list(locked.shot_keys.items())),
        screenshot=screenshot,
        screenshot_label=screenshot_label[:_LABEL_MAX_LENGTH],
        reverts=reverts,
    )
    if reverts is not None:
        reverts.reverted_by = edit
        reverts.save(update_fields=["reverted_by"])
    # robust: a failing broadcast (channel layer down) must not surface after
    # the edit committed, e.g. as the upload service's cleanup of a stored file.
    transaction.on_commit(
        lambda: broadcast_run_and_pivots(run, [pivot]), robust=True
    )
    return edit
