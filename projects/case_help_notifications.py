"""Tell the project's members that a run case needs help (ntfy)."""

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
from projects.models import CaseHelpRequest

logger = logging.getLogger(__name__)


def _build_message(
    help_request: CaseHelpRequest, *, after_auto_fix: bool
) -> NotificationMessage:
    pivot = help_request.test_run_test_case
    test_run = pivot.test_run
    project = test_run.project
    headline = "Auto-fix could not fix it" if after_auto_fix else "Needs help"
    ask = help_request.ask or help_request.reason
    body = f"{pivot.test_case.title}: {ask}" if ask else pivot.test_case.title
    return NotificationMessage.create(
        kind=NotificationKind.NEED_HELP,
        title=f"{headline} · {project.name}",
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
        logger.exception("Could not enqueue the Need help notification")


def notify_case_needs_help(
    help_request_id: int, *, after_auto_fix: bool = False
) -> None:
    """Notify every project member who opted into Need help notifications.

    ``after_auto_fix`` selects the "Auto-fix could not fix it" wording.
    Enqueues ``notifications.tasks.send_notification`` on commit and never
    raises.
    """
    try:
        if not ntfy_configured():
            return
        help_request = (
            CaseHelpRequest.objects.select_related(
                "test_run_test_case__test_case",
                "test_run_test_case__test_run__project",
            )
            .only(
                "ask",
                "reason",
                "test_run_test_case",
                "test_run_test_case__test_case",
                "test_run_test_case__test_case__title",
                "test_run_test_case__test_run",
                "test_run_test_case__test_run__project",
                "test_run_test_case__test_run__project__name",
            )
            .filter(pk=help_request_id)
            .first()
        )
        if help_request is None:
            return
        project = help_request.test_run_test_case.test_run.project
        user_ids = recipient_user_ids(project.pk, NotificationKind.NEED_HELP)
        if not user_ids:
            return
        message = _build_message(help_request, after_auto_fix=after_auto_fix)
        transaction.on_commit(lambda: _enqueue(user_ids, message))
    except Exception:
        logger.exception(
            "Need help notification failed for help request %s", help_request_id
        )
