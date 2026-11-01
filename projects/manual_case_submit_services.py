"""Use case: the tester hands in a claimed Manual run case from the Test Runner.

The hand-in carries the tester's step marks, the case verdict and the list of
screenshots the runner uploads afterwards. Under the project, run and case
locks this module checks the claim, writes one person attempt per marked step
into the checkpoint, sets the case status from the verdict
(``_VERDICT_STATUSES``: never a literal Blocked write), records the
``ManualCaseSubmission`` (the upload and AI-rewrite state), credits the case
to a resolved member, records the session activity and finishes the run when
no case is left. A replay of the same ``submission_id`` returns the screenshot
keys still missing without writing anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Final

from django.db import transaction
from django.utils import timezone

from agents.checkpoint import checkpoint_from_json, checkpoint_to_json
from agents.types import OrchestratorCheckpoint, SubTaskAttempt, SubTaskResult
from controller_client.protocol import (
    ManualCaseSubmitPayload,
    ManualStepOutcome,
    ManualStepResultPayload,
)
from projects.case_credit import credit_cases
from projects.case_duration_services import close_case_segment
from projects.exceptions import ManualRunError, ManualSubmissionDroppedError
from projects.manual_case_lock import claim_matches, lock_manual_run_case
from projects.manual_refinement_services import queue_manual_refinement
from projects.manual_run_finish import finish_manual_run_if_settled
from projects.manual_run_policy import MANUAL_WIRE_VERDICTS, manual_attempt_id
from projects.models import (
    MANUAL_CLAIM_CLEARED,
    MANUAL_CLAIM_FIELDS,
    BlockedReason,
    FailedBy,
    ManualCaseSubmission,
    ManualUploadState,
    ManualVerdict,
    Project,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_actor import PersonStepActor
from projects.services import broadcast_run_and_pivots
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

# The case status each verdict sets (a person's verdict: the only way a manual
# case becomes Blocked).
_VERDICT_STATUSES: Final[dict[ManualVerdict, TestRunTestCaseStatus]] = {
    ManualVerdict.SUCCESS: TestRunTestCaseStatus.SUCCESS,
    ManualVerdict.FAILED: TestRunTestCaseStatus.FAILED,
    ManualVerdict.BLOCKED: TestRunTestCaseStatus.BLOCKED,
}

_DROPPED_UNKNOWN = "This hand-in is not known for this project any more."
_DROPPED_SUPERSEDED = "This case was re-run or reset; the hand-in was discarded."
_DROPPED_CLAIM = "This case is not claimed by the window any more (released, re-run or the run was stopped)."
_RUN_NOT_RUNNING = "This manual run is not running any more."
_NOT_MANUAL_CASE = "This case has no manual steps to hand in."
_STEP_OUT_OF_RANGE = "The hand-in names a step this case does not have."

_PIVOT_SUBMIT_FIELDS: Final[tuple[str, ...]] = (
    "status",
    "result",
    "failed_by",
    "blocked_reason",
    "finished_at",
    "segment_started_at",
    "execution_checkpoint",
    *MANUAL_CLAIM_FIELDS,
    "updated_at",
)


def submit_manual_case(
    *, project: Project, payload: ManualCaseSubmitPayload, actor: PersonStepActor
) -> tuple[str, ...]:
    """Record the hand-in; the screenshot keys the server still needs.

    Idempotent on ``payload.submission_id``. Raises
    ``ManualSubmissionDroppedError`` when the hand-in can never be accepted
    (unknown to this project on replay, superseded, claim released or replaced,
    case no longer In progress) and ``ManualRunError`` for other refusals
    (run not running, a step the case does not have).
    """
    submission_uuid = uuid.UUID(payload.submission_id)
    with transaction.atomic():
        test_run, pivot = lock_manual_run_case(
            project_id=project.pk, pivot_id=payload.pivot_id
        )
        existing = (
            ManualCaseSubmission.objects.filter(submission_id=submission_uuid)
            .only("id", "test_run_test_case_id", "upload_state", "shot_manifest")
            .first()
        )
        if existing is not None:
            return _replay(existing, pivot.pk)
        if not claim_matches(pivot, payload.claim_token) or (
            pivot.status != TestRunTestCaseStatus.IN_PROGRESS
        ):
            raise ManualSubmissionDroppedError(_DROPPED_CLAIM)
        # Permanent refusals drop the hand-in: the runner retries anything else,
        # and a hand-in that can never land would keep its spool (and block
        # Test Runner updates) forever.
        if test_run.status != TestRunStatus.STARTED:
            raise ManualSubmissionDroppedError(_RUN_NOT_RUNNING)
        checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
        if checkpoint is None or not checkpoint.manual:
            raise ManualSubmissionDroppedError(_NOT_MANUAL_CASE)
        step_count = len(checkpoint.plan)
        if any(step.num > step_count for step in payload.steps) or any(
            shot.step_num is not None and shot.step_num > step_count
            for shot in payload.shots
        ):
            raise ManualSubmissionDroppedError(_STEP_OUT_OF_RANGE)

        verdict = MANUAL_WIRE_VERDICTS[payload.verdict]
        now = timezone.now()
        close_case_segment(pivot.pk, now)
        pivot.segment_started_at = None
        pivot.execution_checkpoint = checkpoint_to_json(
            _with_manual_attempts(checkpoint, payload.steps)
        )
        status = _VERDICT_STATUSES[verdict]
        pivot.status = status
        pivot.failed_by = FailedBy.MANUAL if verdict == ManualVerdict.FAILED else ""
        pivot.blocked_reason = (
            BlockedReason.MANUAL_VERDICT if verdict == ManualVerdict.BLOCKED else ""
        )
        pivot.result = payload.verdict_note or _generated_result(
            verdict, payload.steps, step_count
        )
        pivot.finished_at = now
        claim_token = pivot.manual_claim_token
        for field, value in MANUAL_CLAIM_CLEARED.items():
            setattr(pivot, field, value)
        pivot.save(update_fields=list(_PIVOT_SUBMIT_FIELDS))

        shot_count = len(payload.shots)
        submission = ManualCaseSubmission.objects.create(
            test_run_test_case=pivot,
            submission_id=submission_uuid,
            claim_token=claim_token,
            execution_generation=test_run.execution_generation,
            submitted_by=actor.user,
            submitted_label=actor.label(),
            submitted_via=actor.via,
            submitted_at=now,
            verdict=verdict,
            verdict_note=payload.verdict_note,
            shot_manifest=[shot.to_wire() for shot in payload.shots],
            shots_expected=shot_count,
            upload_state=(
                ManualUploadState.UPLOADING
                if shot_count
                else ManualUploadState.COMPLETE
            ),
        )
        if not shot_count:
            queue_manual_refinement(submission.pk)

        # The tester performed and judged the case; an anonymous person at the
        # Test Runner (no resolved member) is neither credited nor recorded.
        credit_cases(user=actor.user, pivot_ids=[pivot.pk])
        record_case_activity(
            user=actor.user, pivot_ids=[pivot.pk], activity=CaseActivity.MANUAL_RESULT
        )
        finish_manual_run_if_settled(test_run)
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    return tuple(shot.key for shot in payload.shots)


def _replay(existing: ManualCaseSubmission, pivot_id: int) -> tuple[str, ...]:
    """The stored outcome of a replayed hand-in: its missing keys (one query)."""
    if existing.test_run_test_case_id != pivot_id:
        raise ManualSubmissionDroppedError(_DROPPED_UNKNOWN)
    if existing.upload_state == ManualUploadState.SUPERSEDED:
        raise ManualSubmissionDroppedError(_DROPPED_SUPERSEDED)
    keys = manifest_keys(existing.shot_manifest)
    if not keys:
        return ()
    stored = set(
        TestRunScreenshot.objects.filter(
            test_run_test_case_id=pivot_id, manual_key__in=keys
        ).values_list("manual_key", flat=True)
    )
    return tuple(key for key in keys if key not in stored)


def manifest_keys(manifest: object) -> list[str]:
    """The screenshot keys of a stored ``shot_manifest``, in order."""
    if not isinstance(manifest, list):
        return []
    keys: list[str] = []
    for entry in manifest:
        if isinstance(entry, dict):
            key = entry.get("key")
            if isinstance(key, str) and key:
                keys.append(key)
    return keys


def _with_manual_attempts(
    checkpoint: OrchestratorCheckpoint, steps: Iterable[ManualStepResultPayload]
) -> OrchestratorCheckpoint:
    """The claim's checkpoint with one person attempt per marked step.

    Unmarked steps get none (the Steps card shows them Not run); the cursor
    moves past the plan, so no step reads as interrupted.
    """
    attempts = tuple(
        SubTaskAttempt(
            step_num=step.num,
            kind="step",
            result=SubTaskResult(
                status="pass" if step.outcome is ManualStepOutcome.PASS else "fail",
                summary=step.note,
                iterations=0,
            ),
            duration_seconds=step.seconds,
            attempt_id=manual_attempt_id(step.shots[0]),
            performed_by="person",
        )
        for step in steps
    )
    return OrchestratorCheckpoint(
        plan=checkpoint.plan,
        attempts=attempts,
        next_step=len(checkpoint.plan) + 1,
        browser=checkpoint.browser,
        total_iterations=0,
        case_step_count=checkpoint.case_step_count,
        case_steps_testrail=checkpoint.case_steps_testrail,
        manual=True,
    )


def _generated_result(
    verdict: ManualVerdict,
    steps: Sequence[ManualStepResultPayload],
    step_count: int,
) -> str:
    """The case's ``result`` when the tester left the case note empty.

    Only Passed, or Failed with a failed step, may come without a note (the
    protocol refuses the rest).
    """
    failed = [step.num for step in steps if step.outcome is ManualStepOutcome.FAIL]
    if verdict == ManualVerdict.FAILED and failed:
        return f"Failed at step {failed[0]} of {step_count}"
    passed = sum(1 for step in steps if step.outcome is ManualStepOutcome.PASS)
    label = "Passed" if verdict == ManualVerdict.SUCCESS else str(verdict.label)
    return f"{label}: {passed} of {step_count} steps passed"
