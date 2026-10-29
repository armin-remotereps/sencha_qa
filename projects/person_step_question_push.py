"""Push a case's Ask AI questions to its case page and nudge its Test Runner.

The case page gets the newest questions of the given step
(``test_run_case.person_step_questions``); the runner is only nudged and
re-reads its snapshot (``runner_person_step_push``). A leaf: models, the question
view and the runner nudge only, so ``person_step_question_services`` can call
it after commit.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from controller_client.protocol import PERSON_STEP_STATE_MAX_QUESTIONS
from projects.models import PersonStepQuestion
from projects.person_step_question_view import build_question_view
from projects.runner_person_step_push import nudge_runner_person_steps

logger = logging.getLogger(__name__)

# Channels event type; ``TestRunCaseConsumer.test_run_case_person_step_questions``.
PERSON_STEP_QUESTIONS_EVENT = "test_run_case.person_step_questions"


def broadcast_person_step_questions(
    pivot_id: int, project_id: int, person_step_id: int
) -> None:
    """Send the case page the step's Q&A, then nudge the runner.

    One query. Call it after commit; never raises.
    """
    try:
        _send_case_questions(pivot_id, person_step_id)
    except Exception:
        logger.exception("Ask AI broadcast failed for pivot %s", pivot_id)
    nudge_runner_person_steps(project_id)


def _send_case_questions(pivot_id: int, person_step_id: int) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    rows = list(
        PersonStepQuestion.objects.filter(person_step_id=person_step_id)
        .select_related("asked_by")
        .order_by("-created_at", "-pk")[:PERSON_STEP_STATE_MAX_QUESTIONS]
    )
    questions = [build_question_view(row).to_socket_dict() for row in reversed(rows)]
    async_to_sync(layer.group_send)(
        f"test_run_case_{pivot_id}",
        {
            "type": PERSON_STEP_QUESTIONS_EVENT,
            "person_step_id": person_step_id,
            "questions": questions,
        },
    )
