"""Prepare which screenshots of checked cases the next TestRail send attaches.

Called when cases are checked (``run_case_check_services.check_locked_pivots``),
the one gate every send passes through; the rules are in
``projects.testrail_evidence``. Query-only (no AI, no file read): a case with
no candidate screenshot is ``done`` at once, a case whose stored review still
matches its inputs (``evidence_review_key``) gets its flags back from the
stored judgments, every other case goes ``pending`` and is queued for the AI
review (``testrail_evidence_review_services``) once the check commits.

Flags and judgments change only while ``testrail_source_result_id IS NULL``
(a checked case can only be checked again after an uncheck, refused once
sent), so a resumed send indexes the same ordered list.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Final

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from agents.checkpoint import checkpoint_from_json, final_step_indices
from agents.types import OrchestratorCheckpoint
from auto_tester.celery import app as celery_app
from projects.manual_current_submission import current_manual_shot_keys
from projects.manual_run_policy import is_manual_checkpoint
from projects.models import (
    EvidenceReviewState,
    TestRunScreenshot,
    TestRunTestCase,
)
from projects.testrail_evidence import (
    OVERLAY_TOOL_NAME,
    EvidenceSelection,
    EvidenceShot,
    ManualMark,
    agent_review_candidates,
    causing_attempt_ids,
    evidence_review_key,
    manual_review_candidates,
    select_reviewed_evidence,
)

logger = logging.getLogger(__name__)

REVIEW_TESTRAIL_EVIDENCE_TASK: Final = "projects.tasks.review_testrail_evidence"
# Cases per queued review task.
REVIEW_TASK_CHUNK_SIZE: Final = 20


@dataclass(frozen=True)
class CaseEvidence:
    """One case's review inputs as they are now (no log, no image bytes).

    ``candidates`` are the shots the AI judges; ``relevance`` holds their
    stored judgments (valid only while ``key`` equals the stored key);
    ``causing`` the causing attempt ids (agent cases).
    """

    pivot_id: int
    status: str
    result: str
    manual: bool
    checkpoint: OrchestratorCheckpoint | None
    candidates: list[EvidenceShot]
    relevance: dict[int, str]
    causing: list[str]
    marks: list[ManualMark]
    key: str
    stored_state: str
    stored_key: str
    shot_names: dict[int, str] = field(default_factory=dict)

    @property
    def stored_review_matches(self) -> bool:
        return (
            self.stored_state == EvidenceReviewState.DONE
            and self.stored_key == self.key
        )

    def selection(self) -> EvidenceSelection:
        """The flags from the stored judgments (call only when they match)."""
        return select_reviewed_evidence(
            self.candidates,
            self.relevance,
            manual=self.manual,
            per_step=settings.TESTRAIL_EVIDENCE_SHOTS_PER_STEP,
            max_shots=(
                settings.MANUAL_TESTRAIL_MAX_SHOTS
                if self.manual
                else settings.TESTRAIL_EVIDENCE_MAX_SHOTS
            ),
        )


def manual_marks(checkpoint: OrchestratorCheckpoint | None) -> list[ManualMark]:
    """Each marked manual step's final ``(status, note)``, by step."""
    if checkpoint is None:
        return []
    attempts = checkpoint.attempts
    return [
        ManualMark(
            step_num=num,
            status=attempts[index].result.status,
            note=attempts[index].result.summary.strip(),
        )
        for num, index in sorted(final_step_indices(attempts).items())
    ]


def load_case_evidence(pivot_ids: Sequence[int]) -> dict[int, CaseEvidence]:
    """The review inputs of these cases, by case id (3 queries whatever the
    number of cases: the cases, the manual hand-ins' keys, the screenshots).

    A case without a source TestRail run is never sent, so it has no
    candidates (``done`` at once, never queued for the AI)."""
    if not pivot_ids:
        return {}
    rows = list(
        TestRunTestCase.objects.filter(id__in=list(pivot_ids)).values_list(
            "id",
            "status",
            "result",
            "execution_checkpoint",
            "evidence_review_state",
            "evidence_review_key",
            "testrail_source_run_id",
        )
    )
    # A case without a source TestRail run is never sent: no candidates.
    sourced = {row[0] for row in rows if row[6] is not None}
    manual_ids = [
        row[0] for row in rows if row[0] in sourced and is_manual_checkpoint(row[3])
    ]
    manual_keys = current_manual_shot_keys(manual_ids)
    manual_set = set(manual_ids)
    shots: dict[int, list[EvidenceShot]] = {row[0]: [] for row in rows}
    relevance: dict[int, str] = {}
    names: dict[int, str] = {}
    shot_rows = (
        TestRunScreenshot.objects.filter(test_run_test_case_id__in=list(sourced))
        .exclude(tool_name=OVERLAY_TOOL_NAME)
        .order_by("created_at", "id")
        .values_list(
            "id",
            "test_run_test_case_id",
            "attempt_id",
            "step_num",
            "tool_name",
            "manual_key",
            "relevance",
            "image",
        )
    )
    for (
        shot_id,
        pivot_id,
        attempt_id,
        step_num,
        tool_name,
        manual_key,
        shot_relevance,
        image_name,
    ) in shot_rows:
        if pivot_id in manual_set:
            # Only the current hand-in's shots; earlier executions' stay.
            if not manual_key or manual_key not in manual_keys.get(
                pivot_id, frozenset()
            ):
                continue
            attempt_id = ""
        shots[pivot_id].append(
            EvidenceShot(
                id=shot_id,
                attempt_id=attempt_id,
                step_num=step_num,
                tool_name=tool_name,
            )
        )
        relevance[shot_id] = shot_relevance
        names[shot_id] = image_name
    evidence: dict[int, CaseEvidence] = {}
    for pivot_id, status, result, checkpoint_json, state, stored_key, _ in rows:
        manual = pivot_id in manual_set
        checkpoint = (
            checkpoint_from_json(checkpoint_json) if pivot_id in sourced else None
        )
        case_shots = shots[pivot_id]
        causing: list[str] = []
        marks: list[ManualMark] = []
        candidates: list[EvidenceShot]
        if pivot_id not in sourced:
            candidates = []
        elif manual:
            candidates = manual_review_candidates(
                status=status,
                shots=case_shots,
                max_shots=settings.MANUAL_TESTRAIL_MAX_SHOTS,
            )
            marks = manual_marks(checkpoint)
        else:
            causing = causing_attempt_ids(
                status=status, checkpoint=checkpoint, shots=case_shots
            )
            candidates = agent_review_candidates(
                status=status,
                checkpoint=checkpoint,
                shots=case_shots,
                max_images=settings.TESTRAIL_EVIDENCE_REVIEW_MAX_IMAGES,
            )
        evidence[pivot_id] = CaseEvidence(
            pivot_id=pivot_id,
            status=status,
            result=result,
            manual=manual,
            checkpoint=checkpoint,
            candidates=candidates,
            relevance={shot.id: relevance[shot.id] for shot in candidates},
            causing=causing,
            marks=marks,
            key=evidence_review_key(
                status=status,
                verdict_text=result,
                candidate_ids=[shot.id for shot in candidates],
                manual_marks=marks,
            ),
            stored_state=state,
            stored_key=stored_key,
            shot_names={shot.id: names[shot.id] for shot in candidates},
        )
    return evidence


def prepare_testrail_evidence(pivots: Sequence[TestRunTestCase]) -> int:
    """Prepare the evidence review of these pivots; returns how many went
    ``pending`` (queued for the AI review once the caller commits).

    ``pivots`` are rows the caller locked (``select_for_update``) in its open
    transaction, loaded with at least ``id``. Six queries whatever the number
    of pivots: the three loads, clear the old flags, set the new ones, one
    bulk update of the review fields.
    """
    if not pivots:
        return 0
    evidence = load_case_evidence([pivot.id for pivot in pivots])
    flagged: list[int] = []
    updates: list[TestRunTestCase] = []
    pending: list[int] = []
    now = timezone.now()
    for case in evidence.values():
        if not case.candidates:
            state, omitted = EvidenceReviewState.DONE, 0
        elif case.stored_review_matches:
            selection = case.selection()
            flagged.extend(selection.ids)
            state, omitted = EvidenceReviewState.DONE, selection.omitted
        else:
            state, omitted = EvidenceReviewState.PENDING, 0
            pending.append(case.pivot_id)
        updates.append(
            TestRunTestCase(
                id=case.pivot_id,
                evidence_review_state=state,
                evidence_review_key=(
                    case.key if state == EvidenceReviewState.DONE else ""
                ),
                evidence_review_error="",
                testrail_evidence_omitted=omitted,
                updated_at=now,
            )
        )
    TestRunScreenshot.objects.filter(
        test_run_test_case_id__in=[pivot.id for pivot in pivots],
        testrail_evidence=True,
    ).update(testrail_evidence=False)
    if flagged:
        TestRunScreenshot.objects.filter(id__in=flagged).update(testrail_evidence=True)
    if updates:
        TestRunTestCase.objects.bulk_update(
            updates,
            [
                "evidence_review_state",
                "evidence_review_key",
                "evidence_review_error",
                "testrail_evidence_omitted",
                "updated_at",
            ],
        )
    queue_evidence_review(pending)
    return len(pending)


def queue_evidence_review(pivot_ids: Sequence[int]) -> None:
    """Queue the AI review of these cases once the caller commits, in chunks
    of ``REVIEW_TASK_CHUNK_SIZE``.

    Best effort: a lost task leaves the cases ``pending``; the next send
    reviews them inline and Retry queues them again.
    """
    ids = list(pivot_ids)
    for start in range(0, len(ids), REVIEW_TASK_CHUNK_SIZE):
        chunk = ids[start : start + REVIEW_TASK_CHUNK_SIZE]
        transaction.on_commit(partial(_send_review_task, chunk))


def _send_review_task(pivot_ids: list[int]) -> None:
    try:
        celery_app.send_task(REVIEW_TESTRAIL_EVIDENCE_TASK, args=[pivot_ids])
    except Exception:
        # Broker unreachable: the cases stay pending for the next send or Retry.
        logger.warning(
            "Could not queue the screenshot review of cases %s",
            pivot_ids,
            exc_info=True,
        )
