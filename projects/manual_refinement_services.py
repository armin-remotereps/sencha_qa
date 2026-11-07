"""The AI rewrite of a Manual run case's notes for TestRail, and the comment parts.

A manual case's TestRail comment is laid out by code
(``testrail_mapping.format_manual_*``); only the tester's step notes and
case note may be reworded, by ``agents.services.manual_result_refiner``.
Statuses, step outcomes, step texts and the Blocked reason label are never
sent for rewriting and always come from the case and its checkpoint.

The rewrite is stored on the case's current hand-in (the newest
non-superseded ``ManualCaseSubmission``) with a hash of its input, made early
by ``refine_manual_case_result`` (queued when the hand-in's uploads finish)
and checked again by every push (``refresh_manual_refinements``). A stored
rewrite is used only while its hash matches; otherwise, and on any AI
failure, the tester's own text is sent.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from agents.checkpoint import checkpoint_from_json, final_step_indices
from agents.exceptions import AIServiceError
from agents.services.llm_config import build_llm_config
from agents.services.manual_result_refiner import (
    ManualRefineInput,
    ManualRefineOutput,
    ManualRefineStep,
    ManualStepOutcome,
    manual_refine_input_hash,
    refine_manual_result,
)
from agents.types import OrchestratorCheckpoint
from auto_tester.celery import app as celery_app
from projects.manual_current_submission import current_manual_submissions
from projects.manual_run_policy import is_manual_checkpoint
from projects.models import (
    MANUAL_SOURCE_MCP,
    ManualCaseSubmission,
    ManualRefineState,
    ManualUploadState,
    TestRun,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.testrail_mapping import (
    ManualStepLine,
    format_manual_shots_note,
    map_pivot_status_to_testrail,
)

logger = logging.getLogger(__name__)

REFINE_MANUAL_CASE_RESULT_TASK: Final = "projects.tasks.refine_manual_case_result"
SWEEP_STALLED_MANUAL_UPLOADS_TASK: Final = "projects.tasks.sweep_stalled_manual_uploads"

_OUTCOME_LABELS: Final[dict[ManualStepOutcome, str]] = {
    "passed": "Passed",
    "failed": "Failed",
    "not_run": "Not run",
}
# Stored rewrites that a push may use as they are (hash permitting).
# Concurrent AI rewrites one push may run.
REFRESH_MAX_WORKERS: Final = 4
_SETTLED_STATES: Final = (ManualRefineState.DONE, ManualRefineState.SKIPPED)


@dataclass(frozen=True)
class ManualCommentParts:
    """The text a push sends for one manual case.

    ``summary`` is the case note to send (the rewrite, else the case's
    ``result`` verbatim); ``steps`` list every verbatim step with its
    checkpoint outcome and note; ``shots_note`` is set when the hand-in's
    screenshots never all arrived. ``refined`` says the rewrite is used.
    """

    tester_label: str
    summary: str
    steps: tuple[ManualStepLine, ...]
    shots_note: str
    refined: bool


# ============================================================================
# QUEUE (called by the screenshot upload service and the stale-upload sweep)
# ============================================================================


def queue_manual_refinement(submission_pk: int) -> None:
    """Queue the rewrite of this hand-in's notes once the caller commits.

    Best effort: a lost task only means the push makes the rewrite inline.
    """
    transaction.on_commit(lambda: _send_refine_task(submission_pk))


def _send_refine_task(submission_pk: int) -> None:
    try:
        celery_app.send_task(REFINE_MANUAL_CASE_RESULT_TASK, args=[submission_pk])
    except Exception:
        # Broker unreachable: the next push refines inline instead.
        logger.warning(
            "Could not queue the manual result rewrite of submission %s",
            submission_pk,
            exc_info=True,
        )


# ============================================================================
# CURRENT HAND-IN AND REWRITE INPUT
# ============================================================================


def _final_outcomes(checkpoint: OrchestratorCheckpoint) -> dict[int, tuple[str, str]]:
    """Each marked step's final ``(status, note)``; unmarked steps are absent."""
    attempts = checkpoint.attempts
    return {
        num: (attempts[index].result.status, attempts[index].result.summary)
        for num, index in final_step_indices(attempts).items()
    }


def _step_outcome(status: str) -> ManualStepOutcome:
    """The tester's outcome of a step from its final attempt status ("" = none)."""
    if status == "pass":
        return "passed"
    if status == "fail":
        return "failed"
    return "not_run"


def _refine_input(
    pivot: TestRunTestCase,
    checkpoint: OrchestratorCheckpoint,
    submission: ManualCaseSubmission,
) -> ManualRefineInput:
    """The rewrite input: verbatim steps, outcomes and notes, and the case note.

    The case note is the case's ``result`` when the tester wrote one; a
    generated line ("Passed: 6 of 6 steps passed") is never reworded.
    """
    outcomes = _final_outcomes(checkpoint)
    steps: list[ManualRefineStep] = []
    for num, sub_task in enumerate(checkpoint.plan, start=1):
        status, note = outcomes.get(num, ("", ""))
        outcome = _step_outcome(status)
        steps.append(
            ManualRefineStep(
                num=num,
                text=sub_task.description,
                expected=sub_task.expected_result,
                outcome=outcome,
                note=note.strip(),
            )
        )
    case_note = pivot.result.strip() if submission.verdict_note.strip() else ""
    return ManualRefineInput(
        case_title=pivot.test_case.title,
        case_outcome=pivot.get_status_display(),
        steps=tuple(steps),
        case_note=case_note,
    )


def _has_valid_rewrite(submission: ManualCaseSubmission, input_hash: str) -> bool:
    return (
        submission.refine_state == ManualRefineState.DONE
        and submission.refined_input_hash == input_hash
    )


def refined_text_is_current(
    submission: ManualCaseSubmission, pivot: TestRunTestCase
) -> bool:
    """The hand-in's stored rewrite is ``done`` and matches the case as it is now.

    Pure (no query): ``pivot`` is the hand-in's case loaded with ``status``,
    ``result``, ``execution_checkpoint`` and ``test_case__title``; the
    hand-in with its note and ``refine_*`` fields. False for a case that is
    no longer a manual result.
    """
    if submission.refine_state != ManualRefineState.DONE:
        return False
    if not is_manual_checkpoint(pivot.execution_checkpoint):
        return False
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    if checkpoint is None:
        return False
    data = _refine_input(pivot, checkpoint, submission)
    return _has_valid_rewrite(submission, manual_refine_input_hash(data))


# ============================================================================
# COMMENT PARTS (called by the TestRail planners; no AI, no writes)
# ============================================================================


def load_manual_comment_parts(
    pivots: Sequence[TestRunTestCase],
) -> dict[int, ManualCommentParts]:
    """The comment parts of the manual cases among ``pivots``, by case id.

    ``pivots`` are loaded with ``status``, ``result``, ``execution_checkpoint``
    and ``test_case__title``. One query (the current hand-ins), none when no
    case is manual. Uses a stored rewrite only while its hash matches.
    """
    manual: list[tuple[TestRunTestCase, OrchestratorCheckpoint]] = []
    for pivot in pivots:
        if not is_manual_checkpoint(pivot.execution_checkpoint):
            continue
        checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
        if checkpoint is not None:
            manual.append((pivot, checkpoint))
    if not manual:
        return {}
    submissions = current_manual_submissions(pivot.id for pivot, _ in manual)
    return {
        pivot.id: _comment_parts(pivot, checkpoint, submissions.get(pivot.id))
        for pivot, checkpoint in manual
    }


def _comment_parts(
    pivot: TestRunTestCase,
    checkpoint: OrchestratorCheckpoint,
    submission: ManualCaseSubmission | None,
) -> ManualCommentParts:
    refined = False
    summary = pivot.result
    step_notes: dict[str, str] = {}
    if submission is not None:
        data = _refine_input(pivot, checkpoint, submission)
        if _has_valid_rewrite(submission, manual_refine_input_hash(data)):
            refined = True
            if data.case_note and submission.refined_summary.strip():
                summary = submission.refined_summary
            if isinstance(submission.refined_step_notes, dict):
                step_notes = {
                    str(key): value
                    for key, value in submission.refined_step_notes.items()
                    if isinstance(value, str) and value.strip()
                }
    outcomes = _final_outcomes(checkpoint)
    steps: list[ManualStepLine] = []
    for num, sub_task in enumerate(checkpoint.plan, start=1):
        status, note = outcomes.get(num, ("", ""))
        outcome = _step_outcome(status)
        # A rewrite only replaces a note the tester wrote.
        if note.strip():
            note = step_notes.get(str(num), note)
        steps.append(
            ManualStepLine(
                num=num,
                text=sub_task.description,
                outcome_label=_OUTCOME_LABELS[outcome],
                note=note,
            )
        )
    shots_note = ""
    if (
        submission is not None
        and submission.upload_state == ManualUploadState.INCOMPLETE
    ):
        shots_note = format_manual_shots_note(
            submission.shots_received,
            submission.shots_expected,
            at_test_runner=submission.source != MANUAL_SOURCE_MCP,
        )
    return ManualCommentParts(
        tester_label=submission.submitted_label if submission is not None else "",
        summary=summary,
        steps=tuple(steps),
        shots_note=shots_note,
        refined=refined,
    )


# ============================================================================
# REWRITE (the Celery task and the pushes)
# ============================================================================


@dataclass(frozen=True)
class _PendingRewrite:
    """A hand-in whose notes need an AI rewrite, with the input it is for."""

    case_id: int
    submission: ManualCaseSubmission
    data: ManualRefineInput
    input_hash: str


def _pending_rewrite(
    pivot: TestRunTestCase,
    checkpoint: OrchestratorCheckpoint,
    submission: ManualCaseSubmission,
) -> _PendingRewrite | None:
    """The rewrite still to make, or None when a settled one matches.

    A hand-in with no notes is stored as ``skipped`` here (no AI call).
    """
    data = _refine_input(pivot, checkpoint, submission)
    input_hash = manual_refine_input_hash(data)
    if (
        submission.refine_state in _SETTLED_STATES
        and submission.refined_input_hash == input_hash
    ):
        return None
    if not data.has_notes:
        _store(submission, ManualRefineState.SKIPPED, input_hash, "", {})
        return None
    return _PendingRewrite(
        case_id=pivot.id, submission=submission, data=data, input_hash=input_hash
    )


def _call_ai(
    data: ManualRefineInput, *, deadline: float
) -> ManualRefineOutput | AIServiceError | None:
    """One AI rewrite with the time left before ``deadline`` (at most
    ``MANUAL_REFINE_TIMEOUT_SECONDS``); None when no time is left.

    No ORM: safe to run in a worker thread. An ``AIServiceError`` is returned,
    never raised.
    """
    remaining = deadline - time.monotonic()
    if remaining < 1:
        return None
    try:
        return refine_manual_result(
            build_llm_config(),
            data,
            timeout_seconds=min(
                float(settings.MANUAL_REFINE_TIMEOUT_SECONDS), remaining
            ),
        )
    except AIServiceError as exc:
        return exc


def _store_outcome(
    pending: _PendingRewrite, outcome: ManualRefineOutput | AIServiceError | None
) -> None:
    """Store a rewrite (``done``) or an AI failure (``failed``: the push sends
    the tester's text and tries again next time); nothing when it never ran."""
    if outcome is None:
        return
    if isinstance(outcome, AIServiceError):
        # Never the notes themselves: they are the tester's text.
        logger.warning(
            "Manual result rewrite failed for case %s (submission %s): %s",
            pending.case_id,
            pending.submission.id,
            outcome.message,
        )
        _store(pending.submission, ManualRefineState.FAILED, pending.input_hash, "", {})
        return
    _store(
        pending.submission,
        ManualRefineState.DONE,
        pending.input_hash,
        outcome.case_note,
        {str(num): text for num, text in outcome.step_notes.items()},
    )


def _store(
    submission: ManualCaseSubmission,
    state: ManualRefineState,
    input_hash: str,
    summary: str,
    step_notes: dict[str, str],
) -> None:
    now = timezone.now()
    ManualCaseSubmission.objects.filter(pk=submission.pk).update(
        refine_state=state,
        refined_summary=summary,
        refined_step_notes=step_notes,
        refined_input_hash=input_hash,
        refined_at=now,
    )
    submission.refine_state = state
    submission.refined_summary = summary
    submission.refined_step_notes = step_notes
    submission.refined_input_hash = input_hash
    submission.refined_at = now


def refine_manual_submission(submission_pk: int) -> None:
    """Rewrite one hand-in's notes early (the ``refine_manual_case_result`` task).

    Does nothing when the hand-in is gone, superseded or no longer the
    case's current one, or the case is no longer a manual result.
    """
    case_id = (
        ManualCaseSubmission.objects.filter(pk=submission_pk)
        .exclude(upload_state=ManualUploadState.SUPERSEDED)
        .values_list("test_run_test_case_id", flat=True)
        .first()
    )
    if case_id is None:
        return
    submission = current_manual_submissions([case_id]).get(case_id)
    if submission is None or submission.pk != submission_pk:
        return
    pivot = (
        TestRunTestCase.objects.select_related("test_case")
        .only("id", "status", "result", "execution_checkpoint", "test_case__title")
        .filter(pk=case_id)
        .first()
    )
    if pivot is None or map_pivot_status_to_testrail(pivot.status) is None:
        return
    checkpoint = (
        checkpoint_from_json(pivot.execution_checkpoint)
        if is_manual_checkpoint(pivot.execution_checkpoint)
        else None
    )
    if checkpoint is None:
        return
    pending = _pending_rewrite(pivot, checkpoint, submission)
    if pending is not None:
        deadline = time.monotonic() + settings.MANUAL_REFINE_TIMEOUT_SECONDS
        _store_outcome(pending, _call_ai(pending.data, deadline=deadline))


def refresh_manual_refinements(
    *,
    test_run: TestRun,
    pivot_ids: Sequence[int] | None,
    budget_seconds: float,
) -> None:
    """Make sure the manual cases a push is about to send have a current rewrite.

    Called by every push before it plans its comments. ``pivot_ids`` limits
    it to those cases (None: every case of the run). Cases whose stored
    rewrite matches are skipped without an AI call; the others are rewritten
    by up to ``REFRESH_MAX_WORKERS`` concurrent AI calls while
    ``budget_seconds`` lasts (each call gets at most
    ``MANUAL_REFINE_TIMEOUT_SECONDS`` and never outlives the budget). Reads
    and writes stay in the calling thread. Past the budget, or on an AI failure,
    the push sends the tester's own text. Never raises for the AI.
    """
    deadline = time.monotonic() + budget_seconds
    scope = Q(execution_checkpoint__manual=True) & Q(
        status__in=(
            TestRunTestCaseStatus.SUCCESS,
            TestRunTestCaseStatus.FAILED,
            TestRunTestCaseStatus.BLOCKED,
            TestRunTestCaseStatus.NEEDS_RETEST,
        )
    )
    if pivot_ids is not None:
        scope &= Q(id__in=list(pivot_ids))
    pivots = list(
        test_run.pivot_entries.filter(scope)
        .select_related("test_case")
        .only("id", "status", "result", "execution_checkpoint", "test_case__title")
        .order_by("id")
    )
    if not pivots:
        return
    submissions = current_manual_submissions(pivot.id for pivot in pivots)
    pending: list[_PendingRewrite] = []
    for pivot in pivots:
        submission = submissions.get(pivot.id)
        checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
        if submission is None or checkpoint is None:
            continue
        item = _pending_rewrite(pivot, checkpoint, submission)
        if item is not None:
            pending.append(item)
    if not pending:
        return
    # The AI calls only (no ORM) run in the pool; every write happens here.
    with ThreadPoolExecutor(
        max_workers=min(REFRESH_MAX_WORKERS, len(pending)),
        thread_name_prefix="manual-refine",
    ) as pool:
        outcomes = list(
            pool.map(lambda item: _call_ai(item.data, deadline=deadline), pending)
        )
    for item, outcome in zip(pending, outcomes):
        _store_outcome(item, outcome)
    skipped = sum(outcome is None for outcome in outcomes)
    if skipped:
        logger.warning(
            "Manual result rewrite budget used up for run %s; sending the "
            "testers' own notes for %s cases",
            test_run.id,
            skipped,
        )


# ============================================================================
# STALE UPLOADS (beat sweep)
# ============================================================================


def sweep_stalled_manual_uploads() -> int:
    """Mark hand-ins ``incomplete`` when no screenshot arrived for
    ``MANUAL_UPLOAD_STALE_HOURS``; returns how many changed.

    A later upload still completes such a hand-in. Each one's rewrite is
    queued, since it may now be checked and sent.
    """
    cutoff = timezone.now() - timedelta(hours=settings.MANUAL_UPLOAD_STALE_HOURS)
    with transaction.atomic():
        stalled = list(
            ManualCaseSubmission.objects.filter(
                upload_state=ManualUploadState.UPLOADING
            )
            .annotate(last_activity=Coalesce("last_shot_at", "submitted_at"))
            .filter(last_activity__lt=cutoff)
            .select_for_update(skip_locked=True)
            .values_list("id", flat=True)
        )
        if not stalled:
            return 0
        changed = ManualCaseSubmission.objects.filter(
            id__in=stalled, upload_state=ManualUploadState.UPLOADING
        ).update(upload_state=ManualUploadState.INCOMPLETE)
        for submission_pk in stalled:
            queue_manual_refinement(submission_pk)
    logger.info("Marked %s manual hand-ins as screenshots incomplete", changed)
    return changed
