"""Read side of the admin case editor and the case's edit history (no writes).

``build_case_edit_page`` gives the editor everything it shows: whether the case
can be edited now (and why not), the base key every form carries, the current
status and summary, the plan steps, the screenshots with their AI relevance,
the admin override and whether TestRail would attach them, and the history.
``list_case_edits`` is the history alone (the case page shows it too).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from projects.case_edit_checkpoint import (
    STEP_FIELD_LABELS,
    StepRow,
    edit_base_key,
    step_rows,
)
from projects.case_edit_policy import (
    FIELD_RESULT,
    FIELD_STATUS,
    FIELD_STEPS,
    UPLOADING,
    edit_blocker,
    is_revertable,
)
from projects.exceptions import CaseEditNotFoundError
from projects.manual_current_submission import uploading_manual_case_ids
from projects.manual_run_policy import is_manual_checkpoint
from projects.models import (
    CASE_EDIT_MAX_UPLOADS_PER_CASE,
    UPLOADED_SCREENSHOT_TOOL_NAME,
    CaseEditKind,
    CaseResultEdit,
    EvidenceOverride,
    ScreenshotRelevance,
    TestRun,
    TestRunScreenshot,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_reopen_services import list_testrail_push_blockers
from projects.testrail_evidence import effective_override

_CHANGE_LABELS: dict[str, str] = {
    **STEP_FIELD_LABELS,
    FIELD_STATUS: "Status",
    FIELD_RESULT: "Summary",
    "summary": "Summary",
    FIELD_STEPS: "Steps",
    "screenshot_attach": "TestRail attach",
    "evidence_override": "TestRail attach",
    "screenshot_deleted": "Screenshot deleted",
    "screenshot_uploaded": "Screenshot uploaded",
}

# Case statuses as the editor names them (the model label of SUCCESS is
# "Success"; people pick "Passed").
_STATUS_LABELS: dict[str, str] = {
    TestRunTestCaseStatus.SUCCESS: "Passed",
    TestRunTestCaseStatus.FAILED: "Failed",
    TestRunTestCaseStatus.BLOCKED: "Blocked",
}

_UPLOADED_CAPTION = "Uploaded"


@dataclass(frozen=True)
class ShotRow:
    """One screenshot of the case as the editor shows it."""

    id: int
    url: str
    caption: str
    step_num: int | None
    created_at: datetime
    relevance: str
    relevance_label: str
    relevance_note: str
    override: str
    override_label: str
    # Whether the next TestRail send would attach it (override first, else
    # the reviewed ``testrail_evidence`` flag).
    will_attach: bool
    testrail_evidence: bool


@dataclass(frozen=True)
class EditChangeRow:
    label: str
    step_num: int | None
    before: str
    after: str


@dataclass(frozen=True)
class EditRow:
    id: int
    kind: str
    kind_label: str
    edited_by_label: str
    edited_at: datetime
    changes: list[EditChangeRow]
    screenshot_label: str
    reverted: bool
    is_revert: bool
    can_revert: bool


@dataclass(frozen=True)
class CaseEditPage:
    editable: bool
    blocker: str | None
    base_key: str
    status: str
    result: str
    step_rows: list[StepRow]
    screenshots: list[ShotRow]
    edits: list[EditRow]
    sent_to_testrail: bool
    upload_slots_left: int


def get_case_for_edit(
    *, project_id: int, run_id: int, pivot_id: int
) -> tuple[TestRun, TestRunTestCase]:
    """The run and the case (logs deferred).

    Raises ``CaseEditNotFoundError`` when the case is not in that project/run.
    """
    pivot = (
        TestRunTestCase.objects.select_related("test_run", "test_case")
        .defer("logs")
        .filter(pk=pivot_id, test_run_id=run_id, test_run__project_id=project_id)
        .first()
    )
    if pivot is None:
        raise CaseEditNotFoundError()
    return pivot.test_run, pivot


def build_case_edit_page(*, pivot: TestRunTestCase, run: TestRun) -> CaseEditPage:
    """The editor's view of the case (3 queries)."""
    shots = list(
        TestRunScreenshot.objects.filter(test_run_test_case_id=pivot.pk).order_by(
            "created_at", "id"
        )
    )
    base_key = edit_base_key(
        pivot, [(shot.pk, shot.evidence_override) for shot in shots]
    )
    blocker = _blocker(pivot, run)
    editable = blocker is None
    uploaded = sum(
        1 for shot in shots if shot.tool_name == UPLOADED_SCREENSHOT_TOOL_NAME
    )
    return CaseEditPage(
        editable=editable,
        blocker=blocker,
        base_key=base_key,
        status=pivot.status,
        result=pivot.result,
        step_rows=step_rows(pivot.execution_checkpoint),
        screenshots=[_shot_row(shot, pivot.finished_at) for shot in shots],
        # An editor that cannot save offers no revert either; "" never matches.
        edits=list_case_edits(pivot, current_key=base_key if editable else ""),
        sent_to_testrail=(
            pivot.testrail_source_result_id is not None
            or pivot.testrail_pushed_at is not None
        ),
        upload_slots_left=max(0, CASE_EDIT_MAX_UPLOADS_PER_CASE - uploaded),
    )


def list_case_edits(
    pivot: TestRunTestCase, *, current_key: str | None = None
) -> list[EditRow]:
    """The case's edit history, newest first (one query).

    ``can_revert`` is set on the newest edit when ``is_revertable`` holds and,
    when ``current_key`` is given, the case still has that edit's after state.
    """
    edits = list(
        CaseResultEdit.objects.filter(test_run_test_case_id=pivot.pk)
        # ``before_state`` holds a whole checkpoint: never loaded here.
        .only(
            "id",
            "kind",
            "edited_by_label",
            "edited_at",
            "changes",
            "after_fingerprint",
            "screenshot_label",
            "reverts_id",
            "reverted_by_id",
        )
        .order_by("-edited_at", "-id")
    )
    rows: list[EditRow] = []
    for index, edit in enumerate(edits):
        can_revert = (
            index == 0
            and is_revertable(edit.kind, edit.reverted_by_id)
            and (current_key is None or current_key == edit.after_fingerprint)
        )
        rows.append(
            EditRow(
                id=edit.pk,
                kind=edit.kind,
                kind_label=_choice_label(CaseEditKind.choices, edit.kind),
                edited_by_label=edit.edited_by_label or "An admin",
                edited_at=edit.edited_at,
                changes=_change_rows(edit.changes),
                screenshot_label=edit.screenshot_label,
                reverted=edit.reverted_by_id is not None,
                is_revert=edit.kind == CaseEditKind.REVERT,
                can_revert=can_revert,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _blocker(pivot: TestRunTestCase, run: TestRun) -> str | None:
    """Why the case cannot be edited now, as ``lock_editable_case`` would say."""
    blocker = edit_blocker(pivot)
    if blocker is not None:
        return blocker
    push_blockers = list_testrail_push_blockers(run)
    if push_blockers:
        return push_blockers[0]
    if is_manual_checkpoint(pivot.execution_checkpoint) and (
        uploading_manual_case_ids([pivot.pk])
    ):
        return UPLOADING
    return None


def _shot_row(shot: TestRunScreenshot, case_finished_at: datetime | None) -> ShotRow:
    # An override set on an earlier verdict of the case no longer applies.
    override = effective_override(
        shot.evidence_override, shot.evidence_override_finished_at, case_finished_at
    )
    if override == EvidenceOverride.INCLUDE:
        will_attach = True
    elif override == EvidenceOverride.EXCLUDE:
        will_attach = False
    else:
        will_attach = shot.testrail_evidence
    return ShotRow(
        id=shot.pk,
        url=shot.image.url if shot.image else "",
        caption=(
            _UPLOADED_CAPTION
            if shot.tool_name == UPLOADED_SCREENSHOT_TOOL_NAME
            else shot.tool_name
        ),
        step_num=shot.step_num,
        created_at=shot.created_at,
        relevance=shot.relevance,
        relevance_label=_choice_label(ScreenshotRelevance.choices, shot.relevance),
        relevance_note=shot.relevance_note,
        override=override,
        override_label=_choice_label(EvidenceOverride.choices, override),
        will_attach=will_attach,
        testrail_evidence=shot.testrail_evidence,
    )


def _choice_label(choices: list[tuple[str, str]], value: str) -> str:
    return dict(choices).get(value, value)


def _change_rows(changes: object) -> list[EditChangeRow]:
    """``CaseResultEdit.changes`` as display rows; malformed entries skipped."""
    if not isinstance(changes, list):
        return []
    rows: list[EditChangeRow] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        field = change.get("field")
        if not isinstance(field, str):
            continue
        step_num = change.get("step_num")
        before = _text(change.get("before"))
        after = _text(change.get("after"))
        if field == FIELD_STATUS:
            before = _STATUS_LABELS.get(before, before)
            after = _STATUS_LABELS.get(after, after)
        rows.append(
            EditChangeRow(
                label=_CHANGE_LABELS.get(field, field),
                step_num=step_num if isinstance(step_num, int) else None,
                before=before,
                after=after,
            )
        )
    return rows


def _text(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
