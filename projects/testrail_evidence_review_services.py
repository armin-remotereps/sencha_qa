"""Review the screenshots of checked cases before they are sent to TestRail.

The use case "review the evidence of checked cases": an AI judges which
candidate screenshots (``projects.testrail_evidence``) show relevant
evidence, from the case title, verdict, the causing steps with a bounded log
excerpt (agent cases) or the tester's marks and notes (manual cases). Only
the relevant ones are flagged ``testrail_evidence``, with an admin's
``evidence_override`` applied through the same selection; none relevant means
none attached. A case is sent (a new result added) only once its review is
``done`` and still current (``testrail_checked_push_services``).

Entry points:

- ``review_checked_evidence``: the ``review_testrail_evidence`` task, queued
  when cases are checked; stands down while the run's send is live (the send
  reviews inline itself).
- ``refresh_evidence_reviews``: inline, by every checked send before it plans.
- ``retry_evidence_reviews``: the Retry action.

The core loads outside any transaction, runs the AI calls in a bounded thread
pool (no ORM in the threads) and writes each case in its own transaction under
the run lock, then the case lock, after checking again that the case is still
checked, unsent and unchanged. Judgments and flags are written only while
``testrail_source_result_id IS NULL``, so a resumed send's attachment index
never moves. Logs carry case ids only, never image data or log text.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Final

from django.conf import settings
from django.db import transaction
from django.db.models import Case, CharField, Value, When
from django.db.models.functions import Right
from django.utils import timezone

from agents.exceptions import AIServiceError
from agents.services.evidence_review import (
    EvidenceImage,
    EvidenceManualStep,
    EvidenceReviewInput,
    EvidenceStepContext,
    review_evidence,
)
from agents.services.llm_config import build_llm_config
from agents.types import OrchestratorCheckpoint
from auto_tester.image_downscale import downscale_for_model
from projects.checked_push_limits import is_checked_push_live
from projects.exceptions import EvidenceReviewError
from projects.manual_current_submission import current_manual_submissions
from projects.models import (
    EVIDENCE_REVIEW_ERROR_MAX_LENGTH,
    SCREENSHOT_RELEVANCE_NOTE_MAX_LENGTH,
    EvidenceReviewState,
    ManualCaseSubmission,
    ScreenshotRelevance,
    TestRun,
    TestRunScreenshot,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.testrail_evidence import causing_attempts
from projects.testrail_evidence_services import (
    REVIEW_TASK_CHUNK_SIZE,
    CaseEvidence,
    load_case_evidence,
    queue_evidence_review,
)

logger = logging.getLogger(__name__)

# Concurrent AI reviews one pass may run.
REVIEW_MAX_WORKERS: Final = 4
# Log characters the AI sees for an agent case (head and tail of the causing
# step's segment); four times as much is read to find the segment.
EVIDENCE_REVIEW_LOG_MAX_CHARS: Final = 12_000
_LOG_TAIL_CHARS: Final = 4 * EVIDENCE_REVIEW_LOG_MAX_CHARS
_LOG_GAP: Final = "\n[…]\n"
_STEP_MARKER: Final = "[Orchestrator] Sub-task {step}/"
_VERDICT_TEXT_MAX_CHARS: Final = 4_000
_IMAGE_MAX_SIDE: Final = 1280
UNREADABLE_IMAGE_NOTE: Final = "unreadable image"
_OUT_OF_TIME_MESSAGE: Final = "The screenshot review ran out of time; retry the review."
_UNEXPECTED_MESSAGE: Final = (
    "The screenshot review failed unexpectedly; retry the review."
)
_LIVE_PUSH_MESSAGE: Final = (
    "Wait for the checked results to finish sending to TestRail."
)
_NOTHING_TO_RETRY_MESSAGE: Final = "No failed screenshot review to retry."
# A pending review is not retried: the queued task or the next send makes it.
_RETRYABLE_STATES: Final = (
    EvidenceReviewState.NONE,
    EvidenceReviewState.FAILED,
)
_VERDICT_LABELS: Final[dict[str, str]] = {
    TestRunTestCaseStatus.SUCCESS: "Passed",
    TestRunTestCaseStatus.FAILED: "Failed",
    TestRunTestCaseStatus.BLOCKED: "Blocked",
}


def review_task_budget_seconds() -> int:
    """Seconds one queued chunk may spend on AI calls (the task's limits add
    a margin on top)."""
    rounds = math.ceil(REVIEW_TASK_CHUNK_SIZE / REVIEW_MAX_WORKERS)
    return settings.TESTRAIL_EVIDENCE_REVIEW_TIMEOUT_SECONDS * rounds


@dataclass(frozen=True)
class ReviewOutcome:
    """What one pass wrote: cases reviewed (``done``), cases whose review
    failed, and cases left as they were (changed, unchecked, sent, or a live
    send on the task path)."""

    reviewed: int = 0
    failed: int = 0
    skipped: int = 0


# ============================================================================
# ENTRY POINTS
# ============================================================================


def review_checked_evidence(pivot_ids: list[int]) -> None:
    """Review these checked cases (the ``review_testrail_evidence`` task).

    Cases of a run whose send is live are left ``pending``: that send reviews
    them inline.
    """
    if not pivot_ids:
        return
    run_ids = sorted(
        set(
            TestRunTestCase.objects.filter(id__in=pivot_ids).values_list(
                "test_run_id", flat=True
            )
        )
    )
    deadline = time.monotonic() + review_task_budget_seconds()
    for run_id in run_ids:
        outcome = _review(
            test_run_id=run_id,
            pivot_ids=pivot_ids,
            deadline=deadline,
            skip_while_live=True,
        )
        logger.info(
            "Screenshot review of run %s: %s reviewed, %s failed, %s skipped",
            run_id,
            outcome.reviewed,
            outcome.failed,
            outcome.skipped,
        )


def refresh_evidence_reviews(
    *,
    test_run: TestRun,
    pivot_ids: Sequence[int] | None,
    budget_seconds: float,
) -> ReviewOutcome:
    """Review the checked, unsent cases a send is about to plan (inline).

    ``pivot_ids`` limits it to those cases (None: every checked case of the
    run). Cases whose stored review still matches are left alone; the
    others are reviewed while ``budget_seconds`` lasts. Never raises for the
    AI: a case that could not be reviewed is ``failed`` and is not sent.
    """
    return _review(
        test_run_id=test_run.id,
        pivot_ids=pivot_ids,
        deadline=time.monotonic() + budget_seconds,
        skip_while_live=False,
    )


def retry_evidence_reviews(*, test_run: TestRun) -> int:
    """Queue the review of the run's checked, unsent cases again; returns how
    many.

    Takes the cases with a source run whose review is ``failed`` or was
    never prepared; ``pending`` ones are left to the queued task or the next
    send (no double AI call), skipped cases wait for an uncheck. Raises
    ``EvidenceReviewError`` while a send is live or when nothing is to retry.
    """
    now = timezone.now()
    with transaction.atomic():
        locked = (
            TestRun.objects.select_for_update()
            .only(
                "id", "testrail_source_push_status", "testrail_source_push_started_at"
            )
            .get(pk=test_run.pk)
        )
        if is_checked_push_live(locked, now):
            raise EvidenceReviewError(_LIVE_PUSH_MESSAGE)
        pivot_ids = list(
            test_run.pivot_entries.select_for_update()
            .filter(
                checked_at__isnull=False,
                testrail_source_result_id__isnull=True,
                testrail_source_run_id__isnull=False,
                testrail_source_skip_reason="",
                evidence_review_state__in=_RETRYABLE_STATES,
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
        if not pivot_ids:
            raise EvidenceReviewError(_NOTHING_TO_RETRY_MESSAGE)
        TestRunTestCase.objects.filter(id__in=pivot_ids).update(
            evidence_review_state=EvidenceReviewState.PENDING,
            evidence_review_key="",
            evidence_review_error="",
            updated_at=now,
        )
        queue_evidence_review(pivot_ids)
    return len(pivot_ids)


# ============================================================================
# CORE
# ============================================================================


@dataclass(frozen=True)
class _ImageRef:
    label: str
    shot_id: int
    step_num: int | None
    kind: str
    name: str


@dataclass(frozen=True)
class _Job:
    """One case to send to the AI: its text input (images added in the
    worker thread) and the candidate image files."""

    case: CaseEvidence
    data: EvidenceReviewInput
    images: tuple[_ImageRef, ...]


# shot id -> (relevance, note) on success; a user-safe message on failure;
# None when the budget ran out before the call.
_Verdict = dict[int, tuple[str, str]] | str | None


def _review(
    *,
    test_run_id: int,
    pivot_ids: Sequence[int] | None,
    deadline: float,
    skip_while_live: bool,
) -> ReviewOutcome:
    scope = TestRunTestCase.objects.filter(
        test_run_id=test_run_id,
        checked_at__isnull=False,
        testrail_source_result_id__isnull=True,
    )
    if pivot_ids is not None:
        scope = scope.filter(id__in=list(pivot_ids))
    ids = sorted(scope.values_list("id", flat=True))
    counts = {EvidenceReviewState.DONE: 0, EvidenceReviewState.FAILED: 0, "": 0}
    # One chunk's checkpoints, log tails and images in memory at a time.
    for start in range(0, len(ids), REVIEW_TASK_CHUNK_SIZE):
        evidence = load_case_evidence(ids[start : start + REVIEW_TASK_CHUNK_SIZE])
        stale = [
            case
            for case in sorted(evidence.values(), key=lambda item: item.pivot_id)
            if not case.stored_review_matches
        ]
        if not stale:
            continue
        # A send that went live reviews these itself: no AI calls here.
        if skip_while_live and _push_is_live(test_run_id):
            counts[""] += len(stale)
            break
        for case in stale:
            # No candidate, or the admin overrode every one: no AI call.
            if not case.needs_ai:
                counts[
                    _write(
                        test_run_id=test_run_id,
                        case=case,
                        verdict={},
                        skip_while_live=skip_while_live,
                    )
                ] += 1
        jobs = _build_jobs([case for case in stale if case.needs_ai])
        if not jobs:
            continue
        # The AI calls and file reads only (no ORM) run in the pool.
        with ThreadPoolExecutor(
            max_workers=min(REVIEW_MAX_WORKERS, len(jobs)),
            thread_name_prefix="evidence-review",
        ) as pool:
            results = list(pool.map(lambda job: _judge(job, deadline), jobs))
        for job, verdict in zip(jobs, results):
            counts[
                _write(
                    test_run_id=test_run_id,
                    case=job.case,
                    verdict=verdict,
                    skip_while_live=skip_while_live,
                )
            ] += 1
    return ReviewOutcome(
        reviewed=counts[EvidenceReviewState.DONE],
        failed=counts[EvidenceReviewState.FAILED],
        skipped=counts[""],
    )


def _push_is_live(test_run_id: int) -> bool:
    run = (
        TestRun.objects.only(
            "id", "testrail_source_push_status", "testrail_source_push_started_at"
        )
        .filter(pk=test_run_id)
        .first()
    )
    return run is None or is_checked_push_live(run, timezone.now())


# ============================================================================
# INPUT
# ============================================================================


def _build_jobs(cases: Sequence[CaseEvidence]) -> list[_Job]:
    """The AI input of each case (one query for titles and agent log tails,
    one for the manual cases' current hand-ins)."""
    if not cases:
        return []
    agent_ids = [case.pivot_id for case in cases if not case.manual]
    submissions = current_manual_submissions(
        case.pivot_id for case in cases if case.manual
    )
    texts = {
        pivot_id: (title, log_tail or "")
        for pivot_id, title, log_tail in TestRunTestCase.objects.filter(
            id__in=[case.pivot_id for case in cases]
        )
        .annotate(
            log_tail=Case(
                When(id__in=agent_ids, then=Right("logs", _LOG_TAIL_CHARS)),
                default=Value(""),
                output_field=CharField(),
            )
        )
        .values_list("id", "test_case__title", "log_tail")
    }
    jobs: list[_Job] = []
    for case in cases:
        if case.pivot_id not in texts:
            continue  # deleted meanwhile
        title, log_tail = texts[case.pivot_id]
        images = tuple(
            _ImageRef(
                label=f"S{index}",
                shot_id=shot.id,
                step_num=shot.step_num,
                kind=shot.tool_name,
                name=case.shot_names.get(shot.id, ""),
            )
            for index, shot in enumerate(case.candidates, start=1)
        )
        steps = () if case.manual else _step_contexts(case)
        jobs.append(
            _Job(
                case=case,
                data=EvidenceReviewInput(
                    case_title=title,
                    verdict_label=_VERDICT_LABELS.get(case.status, case.status),
                    # Manual: the tester's note goes in manual_case_note.
                    verdict_text=(
                        ""
                        if case.manual
                        else case.result.strip()[:_VERDICT_TEXT_MAX_CHARS]
                    ),
                    manual=case.manual,
                    steps=steps,
                    log_excerpt=(
                        ""
                        if case.manual
                        else log_excerpt(
                            log_tail, steps[-1].step_num if steps else None
                        )
                    ),
                    manual_steps=_manual_steps(case) if case.manual else (),
                    manual_case_note=_manual_case_note(
                        case, submissions.get(case.pivot_id)
                    ),
                    images=(),
                    per_step=settings.TESTRAIL_EVIDENCE_SHOTS_PER_STEP,
                ),
                images=images,
            )
        )
    return jobs


def _manual_case_note(
    case: CaseEvidence, submission: ManualCaseSubmission | None
) -> str:
    """The tester's case note: the case's result when they wrote one (a
    generated line such as "Passed: 6 of 6 steps passed" is not a note)."""
    if not case.manual or submission is None or not submission.verdict_note.strip():
        return ""
    return case.result.strip()[:_VERDICT_TEXT_MAX_CHARS]


def _plan_description(checkpoint: OrchestratorCheckpoint | None, step: int) -> str:
    if checkpoint is None or not 1 <= step <= len(checkpoint.plan):
        return ""
    return checkpoint.plan[step - 1].description


def _step_contexts(case: CaseEvidence) -> tuple[EvidenceStepContext, ...]:
    """Each causing step with its failure summary; an interrupted attempt
    (never recorded) is the cursor step, without a summary."""
    checkpoint = case.checkpoint
    recorded = {
        attempt.attempt_id: attempt
        for attempt in causing_attempts(checkpoint, case.causing)
    }
    contexts: list[EvidenceStepContext] = []
    for attempt_id in case.causing:
        attempt = recorded.get(attempt_id)
        if attempt is not None:
            contexts.append(
                EvidenceStepContext(
                    step_num=attempt.step_num,
                    description=_plan_description(checkpoint, attempt.step_num),
                    failure_summary=attempt.result.summary,
                )
            )
        elif checkpoint is not None:
            contexts.append(
                EvidenceStepContext(
                    step_num=checkpoint.next_step,
                    description=_plan_description(checkpoint, checkpoint.next_step),
                    failure_summary="",
                )
            )
    return tuple(contexts)


def _manual_steps(case: CaseEvidence) -> tuple[EvidenceManualStep, ...]:
    """Every verbatim manual step with the tester's final mark and note."""
    if case.checkpoint is None:
        return ()
    marks = {mark.step_num: mark for mark in case.marks}
    steps: list[EvidenceManualStep] = []
    for num, sub_task in enumerate(case.checkpoint.plan, start=1):
        mark = marks.get(num)
        steps.append(
            EvidenceManualStep(
                step_num=num,
                text=sub_task.description,
                mark=mark.status if mark is not None else "",
                note=mark.note if mark is not None else "",
            )
        )
    return tuple(steps)


_STORAGE_UNAVAILABLE_MESSAGE = (
    "The screenshots could not be read from storage; retry the review."
)


def log_excerpt(log_tail: str, step_num: int | None) -> str:
    """From the last ``[Orchestrator] Sub-task {step}/`` marker of the
    causing step to the end, head and tail kept within
    ``EVIDENCE_REVIEW_LOG_MAX_CHARS``; else the plain tail."""
    if step_num is not None:
        start = log_tail.rfind(_STEP_MARKER.format(step=step_num))
        if start >= 0:
            return _head_and_tail(log_tail[start:], EVIDENCE_REVIEW_LOG_MAX_CHARS)
    return log_tail[-EVIDENCE_REVIEW_LOG_MAX_CHARS:]


def _head_and_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = (limit - len(_LOG_GAP)) // 2
    return text[:half] + _LOG_GAP + text[-half:]


# ============================================================================
# AI CALL (worker threads: no ORM)
# ============================================================================


class _StorageUnavailableError(Exception):
    """A screenshot file could not be read for a reason that may pass."""


def _read_image(name: str) -> bytes | None:
    """The downscaled JPEG of a stored screenshot, or None when it is missing
    or cannot be decoded. Raises ``_StorageUnavailableError`` when storage
    fails transiently, so the review fails (and can be retried) instead of
    dropping a real failure screenshot for good."""
    if not name:
        return None
    storage = TestRunScreenshot._meta.get_field("image").storage
    try:
        with storage.open(name, "rb") as handle:
            raw: bytes = handle.read()
    except FileNotFoundError:
        logger.warning("Missing screenshot file for the evidence review")
        return None
    except OSError as exc:
        raise _StorageUnavailableError from exc
    except Exception:
        # Deliberately broad: storage may raise SuspiciousFileOperation and
        # similar for a bad name; that shot is only "unreadable".
        logger.warning("Unreadable screenshot file for the evidence review")
        return None
    try:
        return downscale_for_model(raw, max_side=_IMAGE_MAX_SIDE)
    except Exception:
        # Deliberately broad: a corrupt file makes Pillow raise EOFError,
        # struct.error, IndexError, TypeError and more; one bad shot must
        # only be "unreadable".
        logger.warning("Undecodable screenshot file for the evidence review")
        return None


def _judge(job: _Job, deadline: float) -> _Verdict:
    """Judge one case's candidates with the time left before ``deadline`` (at
    most ``TESTRAIL_EVIDENCE_REVIEW_TIMEOUT_SECONDS``).

    An unreadable image is ``not_relevant`` without being sent; when none is
    readable no AI call is made. Never raises.
    """
    if deadline - time.monotonic() < 1:
        return None
    verdict: dict[int, tuple[str, str]] = {}
    images: list[EvidenceImage] = []
    labels: dict[str, int] = {}
    for ref in job.images:
        try:
            jpeg = _read_image(ref.name)
        except _StorageUnavailableError:
            logger.warning(
                "Screenshot storage unavailable for case %s", job.case.pivot_id
            )
            return _STORAGE_UNAVAILABLE_MESSAGE
        if jpeg is None:
            verdict[ref.shot_id] = (
                ScreenshotRelevance.NOT_RELEVANT,
                UNREADABLE_IMAGE_NOTE,
            )
            continue
        images.append(
            EvidenceImage(
                label=ref.label, step_num=ref.step_num, kind=ref.kind, jpeg=jpeg
            )
        )
        labels[ref.label] = ref.shot_id
    if not images:
        return verdict
    remaining = deadline - time.monotonic()
    if remaining < 1:
        return None
    try:
        judgments = review_evidence(
            build_llm_config(),
            replace(job.data, images=tuple(images)),
            timeout_seconds=min(
                float(settings.TESTRAIL_EVIDENCE_REVIEW_TIMEOUT_SECONDS), remaining
            ),
        )
    except AIServiceError as exc:
        logger.warning(
            "Screenshot review failed for case %s (%s)", job.case.pivot_id, exc.kind
        )
        return exc.message[:EVIDENCE_REVIEW_ERROR_MAX_LENGTH] or _UNEXPECTED_MESSAGE
    except Exception:
        # A worker-thread bug must not abort the other cases' reviews.
        logger.exception("Screenshot review crashed for case %s", job.case.pivot_id)
        return _UNEXPECTED_MESSAGE
    for label, shot_id in labels.items():
        judgment = judgments[label]
        verdict[shot_id] = (
            (
                ScreenshotRelevance.RELEVANT
                if judgment.relevant
                else ScreenshotRelevance.NOT_RELEVANT
            ),
            judgment.reason[:SCREENSHOT_RELEVANCE_NOTE_MAX_LENGTH],
        )
    return verdict


# ============================================================================
# WRITE (calling thread, one transaction per case)
# ============================================================================


def _write(
    *,
    test_run_id: int,
    case: CaseEvidence,
    verdict: _Verdict,
    skip_while_live: bool,
) -> str:
    """Store one case's review; returns the state written, "" when nothing
    was (the case changed, was unchecked or sent, a send went live on the
    task path, or it was reviewed meanwhile)."""
    now = timezone.now()
    with transaction.atomic():
        # Run first, then the case: the order of uncheck and the case send.
        run = (
            TestRun.objects.select_for_update()
            .only(
                "id", "testrail_source_push_status", "testrail_source_push_started_at"
            )
            .filter(pk=test_run_id)
            .first()
        )
        if run is None or (skip_while_live and is_checked_push_live(run, now)):
            return ""
        locked = (
            TestRunTestCase.objects.select_for_update()
            .filter(
                pk=case.pivot_id,
                test_run_id=test_run_id,
                checked_at__isnull=False,
                testrail_source_result_id__isnull=True,
            )
            .values_list("id", flat=True)
            .first()
        )
        if locked is None:
            return ""
        current = load_case_evidence([case.pivot_id]).get(case.pivot_id)
        if current is None or current.key != case.key or current.stored_review_matches:
            return ""
        TestRunScreenshot.objects.filter(
            test_run_test_case_id=case.pivot_id, testrail_evidence=True
        ).update(testrail_evidence=False)
        if verdict is None or isinstance(verdict, str):
            TestRunTestCase.objects.filter(pk=case.pivot_id).update(
                evidence_review_state=EvidenceReviewState.FAILED,
                evidence_review_key=case.key,
                evidence_review_error=(verdict or _OUT_OF_TIME_MESSAGE)[
                    :EVIDENCE_REVIEW_ERROR_MAX_LENGTH
                ],
                testrail_evidence_omitted=0,
                updated_at=now,
            )
            return EvidenceReviewState.FAILED
        if verdict:
            TestRunScreenshot.objects.bulk_update(
                [
                    TestRunScreenshot(
                        id=shot_id, relevance=relevance, relevance_note=note
                    )
                    for shot_id, (relevance, note) in verdict.items()
                ],
                ["relevance", "relevance_note"],
            )
        # The overrides as they are now, under the case lock.
        selection = replace(
            current,
            relevance={
                shot_id: relevance for shot_id, (relevance, _) in verdict.items()
            },
        ).selection()
        if selection.ids:
            TestRunScreenshot.objects.filter(id__in=selection.ids).update(
                testrail_evidence=True
            )
        TestRunTestCase.objects.filter(pk=case.pivot_id).update(
            evidence_review_state=EvidenceReviewState.DONE,
            evidence_review_key=case.key,
            evidence_review_error="",
            testrail_evidence_omitted=selection.omitted,
            updated_at=now,
        )
    return EvidenceReviewState.DONE
