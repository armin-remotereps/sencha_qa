"""Use case: a person asks the AI about the step waiting for them (Ask AI).

Human in the loop person steps, and (D12) the claimed case of a Manual run
asked from the Test Runner window (``ask_manual_step_question``; the
question is anchored on ``manual_case`` and ``manual_step_num``). The AI advises (``agents.services.person_step_advisor``,
read-only knowledge base look-ups and an answer, run by the
``projects.tasks.answer_person_step_question`` task on the ``assist`` queue);
it never acts on the machine and never changes the step, case or run. The web
"Your turn" card and the Test Runner window share these rules and the stored
Q&A (``PersonStepQuestion``); asking is not a test-session activity.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from agents.checkpoint import checkpoint_from_json
from agents.exceptions import AIServiceError
from agents.services.llm_config import build_llm_config
from agents.services.person_step_advisor import answer_person_step_question
from agents.types import PersonHowToItem, PersonStepAdviceContext
from auto_tester.celery import app as celery_app
from controller_client.protocol import PERSON_STEP_STATE_MAX_QUESTIONS
from projects.advice_context import build_advice_context
from projects.exceptions import ManualRunError, PersonStepQuestionError
from projects.manual_advice_mapping import manual_advice_step
from projects.manual_case_lock import claim_matches, lock_manual_run_case
from projects.manual_question_push import broadcast_manual_case_questions
from projects.manual_step_guide_read import load_manual_guide
from projects.models import (
    PERSON_STEP_ANSWER_MAX_LENGTH,
    PERSON_STEP_QUESTION_MAX_LENGTH,
    PERSON_STEP_QUESTIONS_PER_STEP,
    CasePersonStep,
    PersonStepQuestion,
    PersonStepQuestionStatus,
    PersonStepStatus,
    Project,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_actor import PersonStepActor
from projects.person_step_advice_mapping import advice_step, earlier_steps
from projects.person_step_question_push import broadcast_person_step_questions
from projects.person_step_question_view import (
    PersonStepQuestionView,
    build_question_view,
)
from projects.run_reopen_services import lock_project_and_run

logger = logging.getLogger(__name__)

# The Celery task answering a question (``projects.tasks``), sent by name.
ANSWER_PERSON_STEP_QUESTION_TASK = "projects.tasks.answer_person_step_question"

# Questions one project may ask per window, web and Test Runner together.
QUESTION_THROTTLE_LIMIT = 10
QUESTION_THROTTLE_WINDOW_SECONDS = 60
# Newest questions of a step shown on the card and sent to the runner.
QUESTIONS_SHOWN_PER_STEP = PERSON_STEP_STATE_MAX_QUESTIONS
# Shown when the AI call failed (also for a pending question settled as lost).
QUESTION_FAILED_ANSWER = "The AI could not answer right now. Ask again."
# Earlier Q&A of the step the advisor sees.
_HISTORY_MAX_ITEMS = 6
# A pending question older than the LLM timeout plus this is a lost task.
_STALE_GRACE_SECONDS = 60

_THROTTLED = "Too many questions in this project. Wait a minute and ask again."
_NOT_PAUSED = "This run is not waiting for a person."
_STEP_GONE = "This step is no longer waiting for you."
_CONFIRMING = (
    "The agent is checking the report. "
    "You can ask again if the step comes back to you."
)
_EMPTY_QUESTION = "Type a question."
_TOO_LONG = f"Keep the question under {PERSON_STEP_QUESTION_MAX_LENGTH} characters."
_CAP_REACHED = (
    f"This step reached its limit of {PERSON_STEP_QUESTIONS_PER_STEP} questions."
)
_PENDING = "The AI is still answering the last question."
_MANUAL_RUN_NOT_RUNNING = "This manual run is not running."
_MANUAL_NOT_CLAIMED = "This case is not claimed by the window any more."
_MANUAL_NO_STEP = "This case has no such step."
_MANUAL_CAP_REACHED = (
    f"This case reached its limit of {PERSON_STEP_QUESTIONS_PER_STEP} questions."
)

# The statuses a person may ask about (the person's turn).
_ASKABLE_STATUSES: tuple[str, ...] = (
    PersonStepStatus.WAITING,
    PersonStepStatus.NOT_CONFIRMED,
)


@dataclass(frozen=True)
class PersonStepAskState:
    """The Ask AI part of a person step's card."""

    # The newest ``QUESTIONS_SHOWN_PER_STEP``, oldest first.
    questions: tuple[PersonStepQuestionView, ...]
    can_ask: bool
    # Why the person cannot ask now (user-safe); "" when ``can_ask``.
    ask_blocker: str
    # A fresh pending question is the only reason asking is refused.
    has_pending: bool


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def list_person_step_questions(
    step_ids: Sequence[int],
) -> dict[int, tuple[PersonStepQuestionView, ...]]:
    """The newest ``QUESTIONS_SHOWN_PER_STEP`` questions of each step, oldest first.

    One query. Keyed by person step id; a step without questions is absent.
    """
    return {
        step_id: tuple(
            build_question_view(row) for row in rows[-QUESTIONS_SHOWN_PER_STEP:]
        )
        for step_id, rows in _load_questions(step_ids).items()
    }


def get_person_step_ask_state(
    step: CasePersonStep, *, run_paused: bool
) -> PersonStepAskState:
    """The step's Q&A and whether a person may ask now (one query).

    The same checks as ``ask_person_step_question``, without the throttle; a
    stale pending question does not block (the next ask fails it).
    """
    rows = _load_questions([step.pk]).get(step.pk, [])
    questions = tuple(
        build_question_view(row) for row in rows[-QUESTIONS_SHOWN_PER_STEP:]
    )
    blocker = _ask_blocker(step, run_paused=run_paused, rows=rows)
    return PersonStepAskState(
        questions=questions,
        can_ask=not blocker,
        ask_blocker=blocker,
        # Only a pending question that is the sole blocker lets the card offer
        # "Ask another question" once its answer arrives live.
        has_pending=blocker == _PENDING,
    )


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def ask_person_step_question(
    *,
    pivot: TestRunTestCase,
    project_id: int,
    actor: PersonStepActor,
    person_step_id: int,
    question: str,
    expected_round: int | None = None,
) -> PersonStepQuestion:
    """Store a ``pending`` question on the open step and queue its answer.

    ``project_id`` is the project ``pivot`` was resolved in (callers scope
    it). Throttled per project (``QUESTION_THROTTLE_LIMIT`` per
    ``QUESTION_THROTTLE_WINDOW_SECONDS``, shared by both channels). Under the
    project, run and step locks it refuses unless the run is PAUSED, the step
    belongs to ``pivot`` and is ``waiting`` or ``not_confirmed``, its round is
    ``expected_round`` (when given), it has fewer than
    ``PERSON_STEP_QUESTIONS_PER_STEP`` questions and none fresh ``pending``.
    A ``pending`` question older than ``PERSON_STEP_QUESTION_TIMEOUT_SECONDS
    + 60`` is failed first. On commit: sends the answer task and broadcasts
    (``broadcast_person_step_questions``).

    Raises ``PersonStepQuestionError`` with a user-safe message.
    """
    question = question.strip()
    if not question:
        raise PersonStepQuestionError(_EMPTY_QUESTION)
    if len(question) > PERSON_STEP_QUESTION_MAX_LENGTH:
        raise PersonStepQuestionError(_TOO_LONG)
    _check_throttle(project_id)
    with transaction.atomic():
        _project, test_run = lock_project_and_run(pivot.test_run_id)
        if test_run.project_id != project_id:
            raise PersonStepQuestionError(_STEP_GONE)
        if test_run.status != TestRunStatus.PAUSED:
            raise PersonStepQuestionError(_NOT_PAUSED)
        step = (
            CasePersonStep.objects.select_for_update()
            .filter(pk=person_step_id, test_run_test_case_id=pivot.pk)
            .only("id", "status", "round", "test_run_test_case_id")
            .first()
        )
        if step is None or step.status not in _ASKABLE_STATUSES:
            raise PersonStepQuestionError(_STEP_GONE)
        if expected_round is not None and step.round != expected_round:
            raise PersonStepQuestionError(_STEP_GONE)
        rows = list(
            PersonStepQuestion.objects.filter(person_step_id=step.pk).values_list(
                "pk", "status", "created_at"
            )
        )
        if len(rows) >= PERSON_STEP_QUESTIONS_PER_STEP:
            raise PersonStepQuestionError(_CAP_REACHED)
        if _fresh_pending_else_settle(rows, timezone.now()):
            raise PersonStepQuestionError(_PENDING)
        created = PersonStepQuestion.objects.create(
            person_step=step,
            round=step.round,
            question=question,
            asked_by=actor.user,
            asked_via=actor.via,
        )
        question_id = created.pk
        pivot_id = pivot.pk
        step_id = step.pk
        # The enqueue first: a broker failure fails the row before the broadcast.
        transaction.on_commit(lambda: _enqueue_answer(question_id))
        transaction.on_commit(
            lambda: broadcast_person_step_questions(pivot_id, project_id, step_id)
        )
    return created


@dataclass(frozen=True)
class ManualAskState:
    """The Ask AI part of a claimed manual case (the runner snapshot)."""

    # The newest ``QUESTIONS_SHOWN_PER_STEP`` of the current claim, oldest first.
    questions: tuple[PersonStepQuestionView, ...]
    # The cap and the pending rule allow a question now (the claim itself is
    # the caller's check).
    can_ask: bool


def get_manual_ask_state(pivot_id: int, claimed_at: datetime | None) -> ManualAskState:
    """The claimed case's Q&A since ``claimed_at`` and whether a question
    may be asked now (one query; the throttle is not checked)."""
    rows = list(
        PersonStepQuestion.objects.filter(manual_case_id=pivot_id)
        .select_related("asked_by")
        .order_by("created_at", "pk")
    )
    claim_rows = _since_claim(rows, claimed_at)
    stale_before = _stale_before(timezone.now())
    fresh_pending = any(
        row.status == PersonStepQuestionStatus.PENDING
        and row.created_at >= stale_before
        for row in rows
    )
    return ManualAskState(
        questions=tuple(
            build_question_view(row) for row in claim_rows[-QUESTIONS_SHOWN_PER_STEP:]
        ),
        can_ask=not fresh_pending and len(claim_rows) < PERSON_STEP_QUESTIONS_PER_STEP,
    )


def ask_manual_step_question(
    *,
    project: Project,
    pivot_id: int,
    claim_token: str,
    step_num: int | None,
    question: str,
    actor: PersonStepActor,
) -> None:
    """Store a ``pending`` question about the claimed manual case and queue
    its answer (D12).

    The same throttle (shared with person step questions), per-target cap,
    stale-pending rule, task and advisor as ``ask_person_step_question``. The
    gate differs: under the project, run and case locks the run must be a
    running Manual run of ``project``, the case In progress and claimed with
    ``claim_token``; ``step_num`` (None: the whole case) must be one of its
    verbatim steps. The cap counts the current claim's questions. Advisory
    only and not session activity. Raises ``ManualRunError`` (user-safe).
    """
    question = question.strip()
    if not question:
        raise ManualRunError(_EMPTY_QUESTION)
    if len(question) > PERSON_STEP_QUESTION_MAX_LENGTH:
        raise ManualRunError(_TOO_LONG)
    try:
        _check_throttle(project.pk)
    except PersonStepQuestionError as exc:
        raise ManualRunError(exc.message) from exc
    with transaction.atomic():
        test_run, pivot = lock_manual_run_case(project_id=project.pk, pivot_id=pivot_id)
        if test_run.status != TestRunStatus.STARTED:
            raise ManualRunError(_MANUAL_RUN_NOT_RUNNING)
        if pivot.status != TestRunTestCaseStatus.IN_PROGRESS or not claim_matches(
            pivot, claim_token
        ):
            raise ManualRunError(_MANUAL_NOT_CLAIMED)
        if step_num is not None:
            checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
            if checkpoint is None or not 1 <= step_num <= len(checkpoint.plan):
                raise ManualRunError(_MANUAL_NO_STEP)
        rows = list(
            PersonStepQuestion.objects.filter(manual_case_id=pivot.pk).values_list(
                "pk", "status", "created_at"
            )
        )
        claimed_at = pivot.manual_claimed_at
        if (
            sum(
                1
                for _pk, _status, created_at in rows
                if claimed_at is None or created_at >= claimed_at
            )
            >= PERSON_STEP_QUESTIONS_PER_STEP
        ):
            raise ManualRunError(_MANUAL_CAP_REACHED)
        # Over every question of the case: one pending per case (constraint),
        # also one left from an earlier claim.
        if _fresh_pending_else_settle(rows, timezone.now()):
            raise ManualRunError(_PENDING)
        created = PersonStepQuestion.objects.create(
            manual_case=pivot,
            manual_step_num=step_num,
            round=1,
            question=question,
            asked_by=actor.user,
            asked_via=actor.via,
        )
        question_id = created.pk
        project_id = project.pk
        transaction.on_commit(lambda: _enqueue_answer(question_id))
        transaction.on_commit(
            lambda: broadcast_manual_case_questions(pivot_id, project_id)
        )


# ---------------------------------------------------------------------------
# Answering (the Celery task's body)
# ---------------------------------------------------------------------------


def answer_person_step_question_now(question_id: int) -> None:
    """Answer one ``pending`` question (the Celery task's body).

    Idempotent: returns when the question is gone or no longer pending. One
    time budget (``PERSON_STEP_QUESTION_TIMEOUT_SECONDS``) starts here and
    covers building the context and the advisor's calls. The LLM calls run
    outside any transaction; the result is saved with a conditional update
    (``answered``, or ``failed`` on any error, also a soft time limit), then
    broadcast on commit. Stored even if the step closed meanwhile.
    """
    deadline = time.monotonic() + settings.PERSON_STEP_QUESTION_TIMEOUT_SECONDS
    row = (
        PersonStepQuestion.objects.select_related(
            "person_step__test_run_test_case__test_case",
            "person_step__test_run_test_case__test_run__project",
            "manual_case__test_case",
            "manual_case__test_run__project",
        )
        .defer(
            "person_step__test_run_test_case__logs",
            "person_step__test_run_test_case__execution_checkpoint",
            # The manual case's checkpoint holds its verbatim steps: loaded.
            "manual_case__logs",
        )
        .filter(pk=question_id)
        .first()
    )
    if row is None or row.status != PersonStepQuestionStatus.PENDING:
        return
    step = row.person_step
    manual_case = row.manual_case
    build_context: Callable[[], PersonStepAdviceContext]
    broadcast: Callable[[], None]
    if step is not None:
        pivot = step.test_run_test_case
        project_id = pivot.test_run.project_id
        step_id = step.pk
        person_step = step

        def build_context() -> PersonStepAdviceContext:
            return build_advice_context(
                pivot=pivot,
                step=advice_step(person_step),
                earlier_steps=earlier_steps(
                    pivot, before_step_num=person_step.step_num
                ),
                history=_history(step_id, before_id=question_id),
                question=row.question,
            )

        def broadcast() -> None:
            broadcast_person_step_questions(pivot.pk, project_id, step_id)

    elif manual_case is not None:
        # A Manual run question (D12): the claimed case's verbatim step.
        pivot = manual_case
        project_id = pivot.test_run.project_id
        manual_step_num = row.manual_step_num

        def build_context() -> PersonStepAdviceContext:
            return build_advice_context(
                pivot=pivot,
                step=manual_advice_step(
                    pivot,
                    manual_step_num,
                    how_to=_manual_how_to(pivot, manual_step_num),
                ),
                earlier_steps=(),
                history=_manual_history(
                    pivot.pk, manual_step_num, before_id=question_id
                ),
                question=row.question,
            )

        def broadcast() -> None:
            broadcast_manual_case_questions(pivot.pk, project_id)

    else:
        return
    status = PersonStepQuestionStatus.ANSWERED
    try:
        context = build_context()
        answer = answer_person_step_question(
            build_llm_config(),
            context,
            row.question,
            deadline=deadline,
            max_tool_rounds=settings.PERSON_STEP_QUESTION_MAX_TOOL_ROUNDS,
        ).strip()[:PERSON_STEP_ANSWER_MAX_LENGTH]
    except AIServiceError as exc:
        logger.warning("Ask AI question %d not answered: %s", question_id, exc)
        answer = ""
    except SoftTimeLimitExceeded:
        # The person can ask again at once.
        logger.warning("Ask AI question %d hit the time limit", question_id)
        answer = ""
    except Exception:
        logger.exception("Ask AI question %d failed", question_id)
        answer = ""
    if not answer:
        status = PersonStepQuestionStatus.FAILED
        answer = QUESTION_FAILED_ANSWER
    with transaction.atomic():
        updated = PersonStepQuestion.objects.filter(
            pk=question_id, status=PersonStepQuestionStatus.PENDING
        ).update(status=status, answer=answer, answered_at=timezone.now())
        if updated:
            transaction.on_commit(broadcast)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manual_how_to(
    pivot: TestRunTestCase, step_num: int | None
) -> tuple[PersonHowToItem, ...]:
    """Step ``step_num``'s guide items for the advisor; () for the whole case
    or without a guide to show (one query for a step). ``pivot`` has its
    ``test_case`` and ``execution_checkpoint`` loaded."""
    if step_num is None:
        return ()
    guide = load_manual_guide(pivot)
    return guide.step(step_num).how_to if guide is not None else ()


def _load_questions(step_ids: Sequence[int]) -> dict[int, list[PersonStepQuestion]]:
    """Every question of the steps, oldest first (≤ the per-step cap each)."""
    if not step_ids:
        return {}
    grouped: dict[int, list[PersonStepQuestion]] = {}
    rows = (
        PersonStepQuestion.objects.filter(person_step_id__in=list(step_ids))
        .select_related("asked_by")
        .order_by("person_step_id", "created_at", "pk")
    )
    for row in rows:
        # Always set here (filtered on it); None only on manual questions.
        if row.person_step_id is not None:
            grouped.setdefault(row.person_step_id, []).append(row)
    return grouped


def _ask_blocker(
    step: CasePersonStep,
    *,
    run_paused: bool,
    rows: Sequence[PersonStepQuestion],
) -> str:
    """Why a person cannot ask about ``step`` now; "" when they can."""
    if step.status == PersonStepStatus.REPORTED:
        return _CONFIRMING
    if not run_paused:
        return _NOT_PAUSED
    if step.status not in _ASKABLE_STATUSES:
        return _STEP_GONE
    if len(rows) >= PERSON_STEP_QUESTIONS_PER_STEP:
        return _CAP_REACHED
    stale_before = _stale_before(timezone.now())
    if any(
        row.status == PersonStepQuestionStatus.PENDING
        and row.created_at >= stale_before
        for row in rows
    ):
        return _PENDING
    return ""


def _fresh_pending_else_settle(
    rows: Sequence[tuple[int, str, datetime]], now: datetime
) -> bool:
    """True when a ``(pk, status, created_at)`` row is a fresh pending
    question; otherwise its stale pending ones (lost tasks) are failed so the
    target can take a new question."""
    stale_before = _stale_before(now)
    pending = [
        (pk, created_at)
        for pk, status, created_at in rows
        if status == PersonStepQuestionStatus.PENDING
    ]
    if any(created_at >= stale_before for _pk, created_at in pending):
        return True
    if pending:
        PersonStepQuestion.objects.filter(
            pk__in=[pk for pk, _created_at in pending],
            status=PersonStepQuestionStatus.PENDING,
        ).update(
            status=PersonStepQuestionStatus.FAILED,
            answer=QUESTION_FAILED_ANSWER,
            answered_at=now,
        )
    return False


def _since_claim(
    rows: Sequence[PersonStepQuestion], claimed_at: datetime | None
) -> list[PersonStepQuestion]:
    """The questions asked during the current claim (all without one)."""
    if claimed_at is None:
        return list(rows)
    return [row for row in rows if row.created_at >= claimed_at]


def _stale_before(now: datetime) -> datetime:
    return now - timedelta(
        seconds=settings.PERSON_STEP_QUESTION_TIMEOUT_SECONDS + _STALE_GRACE_SECONDS
    )


def _check_throttle(project_id: int) -> None:
    key = f"projects:person-step-question:{project_id}"
    cache.add(key, 0, timeout=QUESTION_THROTTLE_WINDOW_SECONDS)
    try:
        attempts = cache.incr(key)
    except ValueError:
        # The key expired between add and incr: this is the window's first question.
        cache.add(key, 1, timeout=QUESTION_THROTTLE_WINDOW_SECONDS)
        attempts = 1
    if attempts > QUESTION_THROTTLE_LIMIT:
        raise PersonStepQuestionError(_THROTTLED)


def _enqueue_answer(question_id: int) -> None:
    """Send the answer task; on a broker error, fail the question at once."""
    try:
        celery_app.send_task(ANSWER_PERSON_STEP_QUESTION_TASK, args=[question_id])
    except Exception:
        logger.exception("Could not enqueue the answer of question %d", question_id)
        PersonStepQuestion.objects.filter(
            pk=question_id, status=PersonStepQuestionStatus.PENDING
        ).update(
            status=PersonStepQuestionStatus.FAILED,
            answer=QUESTION_FAILED_ANSWER,
            answered_at=timezone.now(),
        )


def _history(step_id: int, *, before_id: int) -> tuple[tuple[str, str], ...]:
    """The step's earlier answered Q&A, oldest first (one query)."""
    rows = list(
        PersonStepQuestion.objects.filter(
            person_step_id=step_id,
            pk__lt=before_id,
            status=PersonStepQuestionStatus.ANSWERED,
        )
        .order_by("-pk")
        .values_list("question", "answer")[:_HISTORY_MAX_ITEMS]
    )
    rows.reverse()
    return tuple((question, answer) for question, answer in rows)


def _manual_history(
    pivot_id: int, step_num: int | None, *, before_id: int
) -> tuple[tuple[str, str], ...]:
    """The case's earlier answered Q&A about the same step, oldest first."""
    rows = list(
        PersonStepQuestion.objects.filter(
            manual_case_id=pivot_id,
            manual_step_num=step_num,
            pk__lt=before_id,
            status=PersonStepQuestionStatus.ANSWERED,
        )
        .order_by("-pk")
        .values_list("question", "answer")[:_HISTORY_MAX_ITEMS]
    )
    rows.reverse()
    return tuple((question, answer) for question, answer in rows)
