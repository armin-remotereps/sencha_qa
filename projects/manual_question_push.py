"""Push a Manual run case's Ask AI questions to its case page and nudge its runner.

The manual twin of ``person_step_question_push``: the case page gets the
newest questions of the case (``test_run_case.person_step_questions`` with
``person_step_id`` 0, which marks a manual case's questions); the runner is only nudged and
re-reads its snapshot. A leaf (models, the question view and the nudge), so
``person_step_question_services`` can call it after commit.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from controller_client.protocol import PERSON_STEP_STATE_MAX_QUESTIONS
from projects.models import PersonStepQuestion
from projects.person_step_question_push import PERSON_STEP_QUESTIONS_EVENT
from projects.person_step_question_view import build_question_view
from projects.runner_person_step_push import nudge_runner_person_steps

logger = logging.getLogger(__name__)


def broadcast_manual_case_questions(pivot_id: int, project_id: int) -> None:
    """Send the case page the case's manual Q&A, then nudge the runner.

    One query. Call it after commit; never raises.
    """
    try:
        _send_case_questions(pivot_id)
    except Exception:
        logger.exception("Manual Ask AI broadcast failed for pivot %s", pivot_id)
    nudge_runner_person_steps(project_id)


def _send_case_questions(pivot_id: int) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    rows = list(
        PersonStepQuestion.objects.filter(manual_case_id=pivot_id)
        .select_related("asked_by")
        .order_by("-created_at", "-pk")[:PERSON_STEP_STATE_MAX_QUESTIONS]
    )
    # Each question also names its verbatim step (null: the whole case).
    questions = [
        {
            **build_question_view(row).to_socket_dict(),
            "manual_step_num": row.manual_step_num,
        }
        for row in reversed(rows)
    ]
    async_to_sync(layer.group_send)(
        f"test_run_case_{pivot_id}",
        {
            "type": PERSON_STEP_QUESTIONS_EVENT,
            "person_step_id": 0,
            "questions": questions,
        },
    )
