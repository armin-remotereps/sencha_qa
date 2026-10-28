"""Tell the project's members that a step waits for them (Human in the loop, ntfy).

Pushes go to the "Need help" preference: a person step is the same kind of
"a run waits for you" moment. The caller decides when to push (a hand-off
that starts a new sequence of person steps in the case, and every round the
agent could not confirm); a plain next step after one just passed updates the
page live instead. Topics are never logged.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.urls import reverse

from notifications.services.notification_message import (
    NotificationKind,
    NotificationMessage,
)
from notifications.services.ntfy_config import ntfy_configured
from notifications.services.recipients import recipient_user_ids
from notifications.tasks import send_notification
from projects.models import CasePersonStep, PersonStepStatus

logger = logging.getLogger(__name__)

# The instruction is model-written and can be long; the push shows its head.
_INSTRUCTION_HEAD_MAX = 160


def _instruction_head(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= _INSTRUCTION_HEAD_MAX:
        return flat
    return flat[: _INSTRUCTION_HEAD_MAX - 1].rstrip() + "…"


def _build_message(person_step: CasePersonStep) -> NotificationMessage:
    pivot = person_step.test_run_test_case
    test_run = pivot.test_run
    project = test_run.project
    if person_step.status == PersonStepStatus.NOT_CONFIRMED:
        detail = f"the agent could not confirm step {person_step.step_num}"
    else:
        detail = f"step {person_step.step_num}"
    head = _instruction_head(person_step.instruction)
    body = f"{pivot.test_case.title}: {detail}"
    if head:
        body += f" — {head}"
    return NotificationMessage.create(
        kind=NotificationKind.NEED_HELP,
        title=f"Your turn · {project.name}",
        body=body,
        path=reverse(
            "projects:test_run_case_detail",
            kwargs={
                "project_id": project.pk,
                "test_run_id": test_run.pk,
                "pivot_id": pivot.pk,
            },
        ),
    )


def _enqueue(user_ids: list[int], message: NotificationMessage) -> None:
    try:
        send_notification.delay(
            user_ids, message.kind.value, message.title, message.body, message.path
        )
    except Exception:
        logger.exception("Could not enqueue the person step notification")


def notify_person_step(person_step_id: int) -> None:
    """Notify every project member who opted into Need help notifications.

    Enqueues ``notifications.tasks.send_notification`` on commit and never
    raises.
    """
    try:
        if not ntfy_configured():
            return
        person_step = (
            CasePersonStep.objects.select_related(
                "test_run_test_case__test_case",
                "test_run_test_case__test_run__project",
            )
            .only(
                "step_num",
                "status",
                "instruction",
                "test_run_test_case",
                "test_run_test_case__test_case",
                "test_run_test_case__test_case__title",
                "test_run_test_case__test_run",
                "test_run_test_case__test_run__project",
                "test_run_test_case__test_run__project__name",
            )
            .filter(pk=person_step_id)
            .first()
        )
        if person_step is None:
            return
        project = person_step.test_run_test_case.test_run.project
        user_ids = recipient_user_ids(project.pk, NotificationKind.NEED_HELP)
        if not user_ids:
            return
        message = _build_message(person_step)
        transaction.on_commit(lambda: _enqueue(user_ids, message))
    except Exception:
        logger.exception(
            "Person step notification failed for person step %s", person_step_id
        )
