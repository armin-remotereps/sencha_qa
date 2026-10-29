"""Use case: a person asks the AI about the step waiting for them (Ask AI).

Human in the loop only. The AI advises (one tool-free LLM call, run by the
``projects.tasks.answer_person_step_question`` task on the ``assist`` queue);
it never acts on the machine and never changes the step, case or run. The web
"Your turn" card and the Test Runner window share these rules and the stored
Q&A (``PersonStepQuestion``); asking is not a test-session activity.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from agents.exceptions import AIServiceError
from agents.os_shell import get_os_name, shell_label
from agents.services.llm_config import build_llm_config
from agents.services.person_step_advisor import answer_person_step_question
from agents.types import PersonStepAdviceContext, PersonStepHandOff
from auto_tester.celery import app as celery_app
from controller_client.protocol import PERSON_STEP_STATE_MAX_QUESTIONS
from projects.case_task_text import build_task_description
from projects.exceptions import PersonStepQuestionError
from projects.models import (
    PERSON_STEP_ANSWER_MAX_LENGTH,
    PERSON_STEP_QUESTION_MAX_LENGTH,
    PERSON_STEP_QUESTIONS_PER_STEP,
    CasePersonStep,
    PersonStepQuestion,
    PersonStepQuestionStatus,
    PersonStepStatus,
    TestRunStatus,
    TestRunTestCase,
)
from projects.person_step_actor import PersonStepActor
from projects.person_step_question_push import broadcast_person_step_questions
from projects.person_step_question_view import (
    PersonStepQuestionView,
    build_question_view,
)
from projects.person_step_rows import stored_hand_off_kind, stored_how_to
from projects.run_reopen_services import lock_project_and_run
from projects.services import build_agent_project_context

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
# Earlier Q&A of the step the advisor sees, and the application context cap.
_HISTORY_MAX_ITEMS = 6
_PROJECT_CONTEXT_MAX_LENGTH = 4000
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
        step_id: tuple(build_question_view(row) for row in rows[-QUESTIONS_SHOWN_PER_STEP:])
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
    questions = tuple(build_question_view(row) for row in rows[-QUESTIONS_SHOWN_PER_STEP:])
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
        now = timezone.now()
        stale_before = _stale_before(now)
        pending = [
            (pk, created_at)
            for pk, status, created_at in rows
            if status == PersonStepQuestionStatus.PENDING
        ]
        if any(created_at >= stale_before for _pk, created_at in pending):
            raise PersonStepQuestionError(_PENDING)
        if pending:
            # A lost task: settle it so the step can take a new question.
            PersonStepQuestion.objects.filter(
                pk__in=[pk for pk, _created_at in pending],
                status=PersonStepQuestionStatus.PENDING,
            ).update(
                status=PersonStepQuestionStatus.FAILED,
                answer=QUESTION_FAILED_ANSWER,
                answered_at=now,
            )
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


# ---------------------------------------------------------------------------
# Answering (the Celery task's body)
# ---------------------------------------------------------------------------


def answer_person_step_question_now(question_id: int) -> None:
    """Answer one ``pending`` question (the Celery task's body).

    Idempotent: returns when the question is gone or no longer pending. The
    LLM call runs outside any transaction; the result is saved with a
    conditional update (``answered``, or ``failed`` on ``AIServiceError``),
    then broadcast on commit. Stored even if the step closed meanwhile.
    """
    row = (
        PersonStepQuestion.objects.select_related(
            "person_step__test_run_test_case__test_case",
            "person_step__test_run_test_case__test_run__project",
        )
        .defer(
            "person_step__test_run_test_case__logs",
            "person_step__test_run_test_case__execution_checkpoint",
        )
        .filter(pk=question_id)
        .first()
    )
    if row is None or row.status != PersonStepQuestionStatus.PENDING:
        return
    step = row.person_step
    pivot = step.test_run_test_case
    project = pivot.test_run.project
    history = _history(step.pk, before_id=row.pk)
    context = _advice_context(step, pivot, history)
    status = PersonStepQuestionStatus.ANSWERED
    try:
        answer = answer_person_step_question(
            build_llm_config(),
            context,
            row.question,
            timeout_seconds=settings.PERSON_STEP_QUESTION_TIMEOUT_SECONDS,
        ).strip()[:PERSON_STEP_ANSWER_MAX_LENGTH]
    except AIServiceError as exc:
        logger.warning("Ask AI question %d not answered: %s", question_id, exc)
        answer = ""
    except Exception:
        # Also a soft time limit: the person can ask again at once.
        logger.exception("Ask AI question %d failed", question_id)
        answer = ""
    if not answer:
        status = PersonStepQuestionStatus.FAILED
        answer = QUESTION_FAILED_ANSWER
    pivot_id = pivot.pk
    project_id = project.pk
    step_id = step.pk
    with transaction.atomic():
        updated = PersonStepQuestion.objects.filter(
            pk=question_id, status=PersonStepQuestionStatus.PENDING
        ).update(status=status, answer=answer, answered_at=timezone.now())
        if updated:
            transaction.on_commit(
                lambda: broadcast_person_step_questions(
                    pivot_id, project_id, step_id
                )
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _advice_context(
    step: CasePersonStep,
    pivot: TestRunTestCase,
    history: tuple[tuple[str, str], ...],
) -> PersonStepAdviceContext:
    project = pivot.test_run.project
    system_info = project.agent_system_info
    os_name = get_os_name(system_info if isinstance(system_info, dict) else None)
    project_context = build_agent_project_context(project) or ""
    return PersonStepAdviceContext(
        os_name=os_name,
        shell_label=shell_label(os_name),
        case_text=build_task_description(pivot.test_case),
        project_prompt=project_context[:_PROJECT_CONTEXT_MAX_LENGTH],
        # The checkpoint (with the plan's size) is not loaded here.
        step_label=f"Step {step.step_num}",
        hand_off=PersonStepHandOff(
            step_num=step.step_num,
            kind=stored_hand_off_kind(step.kind),
            instruction=step.instruction,
            expected_result=step.expected_result,
            command=step.command,
            objection=step.agent_objection,
            how_to=stored_how_to(step.how_to),
        ),
        history=history,
    )
