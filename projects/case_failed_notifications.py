"""Tell the project's members that the agent judged a run case Failed (ntfy)."""

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
from projects.models import TestRunTestCase, TestRunTestCaseStatus

logger = logging.getLogger(__name__)


def _build_message(pivot: TestRunTestCase) -> NotificationMessage:
    test_run = pivot.test_run
    project = test_run.project
    verdict = pivot.result
    title = pivot.test_case.title
    return NotificationMessage.create(
        kind=NotificationKind.FAILED,
        title=f"Failed · {project.name}",
        body=f"{title}: {verdict}" if verdict.strip() else title,
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
        logger.exception("Could not enqueue the Failed notification")


def notify_case_failed(pivot_id: int, *, generation: int) -> None:
    """Notify every project member who opted into Failed notifications.

    Only for the agent's verdict: call it right after the verdict is saved.
    Sends nothing unless the case is still Failed and the run is still on
    ``generation`` (a newer Start, Resume or Re-run owns it otherwise).
    Enqueues ``notifications.tasks.send_notification`` on commit and never
    raises.
    """
    try:
        if not ntfy_configured():
            return
        pivot = (
            TestRunTestCase.objects.select_related("test_case", "test_run__project")
            .only(
                "result",
                "test_case",
                "test_case__title",
                "test_run",
                "test_run__project",
                "test_run__project__name",
            )
            .filter(
                pk=pivot_id,
                status=TestRunTestCaseStatus.FAILED,
                test_run__execution_generation=generation,
            )
            .first()
        )
        if pivot is None:
            return
        user_ids = recipient_user_ids(
            pivot.test_run.project_id, NotificationKind.FAILED
        )
        if not user_ids:
            return
        message = _build_message(pivot)
        transaction.on_commit(lambda: _enqueue(user_ids, message))
    except Exception:
        logger.exception("Failed notification failed for pivot %s", pivot_id)
