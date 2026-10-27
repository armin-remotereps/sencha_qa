"""Freeze which screenshots of checked cases the next TestRail send attaches.

Called when cases are checked (``run_case_check_services.check_run_cases``),
the one gate every send passes through; the rules are in
``projects.testrail_evidence``. The flags then stay fixed until the case is
unchecked or reopened, so a resumed send indexes the same ordered list.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.conf import settings

from agents.checkpoint import checkpoint_from_json
from projects.models import (
    TestRunScreenshot,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.testrail_evidence import (
    OVERLAY_TOOL_NAME,
    EvidenceShot,
    select_evidence,
)

_EVIDENCE_STATUSES = (TestRunTestCaseStatus.FAILED, TestRunTestCaseStatus.BLOCKED)


def freeze_testrail_evidence(pivots: Sequence[TestRunTestCase]) -> None:
    """Set ``testrail_evidence`` on the selected screenshots of these pivots.

    ``pivots`` are rows the caller locked (``select_for_update``) in its open
    transaction, loaded with at least ``id`` and ``status``. Only Failed and
    Blocked cases attach screenshots, so only their checkpoints and screenshot
    rows are loaded. At most four queries whatever the number of pivots: the
    checkpoints, the screenshot rows, clear the old flags, set the new ones.
    """
    if not pivots:
        return
    with_evidence = [pivot.id for pivot in pivots if pivot.status in _EVIDENCE_STATUSES]
    selected = _select(with_evidence) if with_evidence else []
    TestRunScreenshot.objects.filter(
        test_run_test_case_id__in=[pivot.id for pivot in pivots],
        testrail_evidence=True,
    ).update(testrail_evidence=False)
    if selected:
        TestRunScreenshot.objects.filter(id__in=selected).update(testrail_evidence=True)


def _select(pivot_ids: list[int]) -> list[int]:
    statuses_and_checkpoints = {
        pivot_id: (status, checkpoint_json)
        for pivot_id, status, checkpoint_json in TestRunTestCase.objects.filter(
            id__in=pivot_ids
        ).values_list("id", "status", "execution_checkpoint")
    }
    shots_by_pivot: dict[int, list[EvidenceShot]] = {
        pivot_id: [] for pivot_id in pivot_ids
    }
    rows = (
        TestRunScreenshot.objects.filter(test_run_test_case_id__in=pivot_ids)
        .exclude(tool_name=OVERLAY_TOOL_NAME)
        .order_by("created_at", "id")
        .values_list("id", "test_run_test_case_id", "attempt_id", "step_num")
    )
    for shot_id, pivot_id, attempt_id, step_num in rows:
        shots_by_pivot[pivot_id].append(
            EvidenceShot(id=shot_id, attempt_id=attempt_id, step_num=step_num)
        )
    selected: list[int] = []
    for pivot_id, (status, checkpoint_json) in statuses_and_checkpoints.items():
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
