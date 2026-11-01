"""Freeze which screenshots of checked cases the next TestRail send attaches.

Called when cases are checked (``run_case_check_services.check_run_cases``),
the one gate every send passes through; the rules are in
``projects.testrail_evidence``. The flags then stay fixed until the case is
unchecked or reopened, so a resumed send indexes the same ordered list.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.conf import settings
from django.db.models import Q

from agents.checkpoint import checkpoint_from_json
from projects.manual_current_submission import current_manual_shot_keys
from projects.manual_run_policy import is_manual_checkpoint
from projects.models import (
    TestRunScreenshot,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.testrail_evidence import (
    OVERLAY_TOOL_NAME,
    EvidenceShot,
    ManualEvidenceShot,
    select_evidence,
    select_manual_evidence,
)

_EVIDENCE_STATUSES = (TestRunTestCaseStatus.FAILED, TestRunTestCaseStatus.BLOCKED)
# Manual cases also attach their screenshots when Passed (D7).
_CANDIDATE_STATUSES = (*_EVIDENCE_STATUSES, TestRunTestCaseStatus.SUCCESS)


def freeze_testrail_evidence(pivots: Sequence[TestRunTestCase]) -> None:
    """Set ``testrail_evidence`` on the selected screenshots of these pivots.

    ``pivots`` are rows the caller locked (``select_for_update``) in its open
    transaction, loaded with at least ``id`` and ``status``. Failed and
    Blocked cases attach screenshots, and manual cases (``manual`` checkpoint)
    also when Passed, so only their checkpoints and screenshot rows are
    loaded. At most five queries whatever the number of pivots: the
    checkpoints, the manual hand-ins' keys, the screenshot rows, clear the old
    flags, set the new ones.
    """
    if not pivots:
        return
    candidates = [pivot.id for pivot in pivots if pivot.status in _CANDIDATE_STATUSES]
    selected = _select(candidates) if candidates else []
    TestRunScreenshot.objects.filter(
        test_run_test_case_id__in=[pivot.id for pivot in pivots],
        testrail_evidence=True,
    ).update(testrail_evidence=False)
    if selected:
        TestRunScreenshot.objects.filter(id__in=selected).update(testrail_evidence=True)


def _select(pivot_ids: list[int]) -> list[int]:
    # A Passed case's checkpoint is loaded only when it is a manual one.
    statuses_and_checkpoints = {
        pivot_id: (status, checkpoint_json)
        for pivot_id, status, checkpoint_json in TestRunTestCase.objects.filter(
            Q(status__in=_EVIDENCE_STATUSES)
            | Q(
                status=TestRunTestCaseStatus.SUCCESS, execution_checkpoint__manual=True
            ),
            id__in=pivot_ids,
        ).values_list("id", "status", "execution_checkpoint")
    }
    manual_ids = [
        pivot_id
        for pivot_id, (_, checkpoint_json) in statuses_and_checkpoints.items()
        if is_manual_checkpoint(checkpoint_json)
    ]
    manual_keys = current_manual_shot_keys(manual_ids)
    shots_by_pivot: dict[int, list[EvidenceShot]] = {
        pivot_id: [] for pivot_id in statuses_and_checkpoints
    }
    manual_shots: dict[int, list[ManualEvidenceShot]] = {
        pivot_id: [] for pivot_id in manual_ids
    }
    rows = (
        TestRunScreenshot.objects.filter(
            test_run_test_case_id__in=list(statuses_and_checkpoints)
        )
        .exclude(tool_name=OVERLAY_TOOL_NAME)
        .order_by("created_at", "id")
        .values_list(
            "id",
            "test_run_test_case_id",
            "attempt_id",
            "step_num",
            "tool_name",
            "manual_key",
        )
    )
    for shot_id, pivot_id, attempt_id, step_num, tool_name, manual_key in rows:
        if pivot_id in manual_shots:
            # Only the current hand-in's shots; earlier executions' stay.
            if manual_key and manual_key in manual_keys.get(pivot_id, frozenset()):
                manual_shots[pivot_id].append(
                    ManualEvidenceShot(
                        id=shot_id, tool_name=tool_name, step_num=step_num
                    )
                )
            continue
        shots_by_pivot[pivot_id].append(
            EvidenceShot(id=shot_id, attempt_id=attempt_id, step_num=step_num)
        )
    selected: list[int] = []
    for pivot_id, (status, checkpoint_json) in statuses_and_checkpoints.items():
        if pivot_id in manual_shots:
            selected.extend(
                select_manual_evidence(
                    status=status,
                    shots=manual_shots[pivot_id],
                    max_shots=settings.MANUAL_TESTRAIL_MAX_SHOTS,
                )
            )
            continue
        selected.extend(
            select_evidence(
                status=status,
                checkpoint=checkpoint_from_json(checkpoint_json),
                shots=shots_by_pivot[pivot_id],
                per_step=settings.TESTRAIL_EVIDENCE_SHOTS_PER_STEP,
                max_shots=settings.TESTRAIL_EVIDENCE_MAX_SHOTS,
            )
        )
    return selected
