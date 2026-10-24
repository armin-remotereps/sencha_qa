"""Tell the project's members that a run case needs help (Web Push)."""

from __future__ import annotations

import logging

from django.db import transaction
from django.urls import reverse

from notifications.services.push_config import push_configured
from notifications.services.push_message import PushMessage
from notifications.tasks import send_push
from projects.models import CaseHelpRequest

logger = logging.getLogger(__name__)

# Push services and lock screens truncate long bodies anyway.
PUSH_BODY_MAX_LENGTH = 180


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _build_message(
    help_request: CaseHelpRequest, *, after_auto_fix: bool
) -> PushMessage:
    pivot = help_request.test_run_test_case
    test_run = pivot.test_run
    project = test_run.project
    headline = "Auto-fix could not fix it" if after_auto_fix else "Needs help"
    ask = help_request.ask or help_request.reason
    body = f"{pivot.test_case.title}: {ask}" if ask else pivot.test_case.title
    return PushMessage(
        title=_truncate(f"{headline} · {project.name}", PUSH_BODY_MAX_LENGTH),
        body=_truncate(body, PUSH_BODY_MAX_LENGTH),
        url=reverse(
            "projects:test_run_case_detail",
            kwargs={
                "project_id": project.pk,
                "test_run_id": test_run.pk,
                "pivot_id": pivot.pk,
            },
        ),
        tag=f"need-help-{pivot.pk}",
    )


def _enqueue(user_ids: list[int], message: PushMessage) -> None:
    try:
        send_push.delay(user_ids, message.title, message.body, message.url, message.tag)
    except Exception:
        logger.exception("Could not enqueue the Need help push")


def notify_case_needs_help(
    help_request_id: int, *, after_auto_fix: bool = False
) -> None:
    """Push "needs help" to every project member with notifications enabled.

    ``after_auto_fix`` selects the "Auto-fix could not fix it" wording. Enqueues
    ``notifications.tasks.send_push`` and never raises.
    """
    try:
        if not push_configured():
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
        # Members with at least one device; one query.
        user_ids = list(
            project.members.filter(push_subscriptions__isnull=False)
            .distinct()
            .values_list("id", flat=True)
        )
        if not user_ids:
            return
        message = _build_message(help_request, after_auto_fix=after_auto_fix)
        transaction.on_commit(lambda: _enqueue(user_ids, message))
    except Exception:
        logger.exception(
            "Need help notification failed for help request %s", help_request_id
        )
