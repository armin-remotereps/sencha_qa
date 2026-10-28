"""Tell the project's members that the agent judged a run case Failed.

Every active member gets a missed failure in their attention inbox (and a
live nudge); members who opted in also get an ntfy push.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from notifications.services.enqueue_notification import enqueue_notification
from notifications.services.inbox_broadcast import broadcast_inbox_changed
from notifications.services.notification_message import (
    NotificationKind,
    NotificationMessage,
)
from notifications.services.ntfy_config import ntfy_configured
from notifications.services.recipients import recipient_user_ids
from notifications.services.record_missed_failure import record_missed_failure
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


def notify_case_failed(pivot_id: int, *, generation: int) -> None:
    """Record the failure in every member's inbox, then push to those opted in.

    Only for the agent's verdict: call it right after the verdict is saved.
    Does nothing unless the case is still Failed and the run is still on
    ``generation`` (a newer Start, Resume or Re-run owns it otherwise). The
    inbox rows are written whether or not ntfy is configured; the live nudge
    and ``notifications.tasks.send_notification`` run on commit. Never raises.
    """
    try:
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
        project_id = pivot.test_run.project_id
        member_ids = record_missed_failure(
            pivot_id=pivot.pk, project_id=project_id, occurred_at=timezone.now()
        )
        if member_ids:
            transaction.on_commit(lambda: broadcast_inbox_changed(member_ids))
        if not ntfy_configured():
            return
        user_ids = recipient_user_ids(project_id, NotificationKind.FAILED)
        if not user_ids:
            return
        message = _build_message(pivot)
        transaction.on_commit(lambda: enqueue_notification(user_ids, message))
    except Exception:
        logger.exception("Failed notification failed for pivot %s", pivot_id)
