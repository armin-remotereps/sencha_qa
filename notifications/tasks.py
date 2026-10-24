from __future__ import annotations

import logging

from celery import shared_task

from notifications.services.notification_message import (
    NotificationKind,
    NotificationMessage,
)
from notifications.services.send_notification import send_to_users

logger = logging.getLogger(__name__)


@shared_task(
    name="notifications.tasks.send_notification",
    # Its own queue and worker (CELERY_TASK_ROUTES): notifications are
    # time-sensitive and must not wait behind TestRail imports.
    queue="notify",
    max_retries=0,
    # Each request is bounded by NTFY_REQUEST_TIMEOUT; sends run in parallel.
    soft_time_limit=60,
    time_limit=90,
)
def send_notification(
    user_ids: list[int], kind: str, title: str, body: str, path: str
) -> None:
    """Send one notification to the users' ntfy topics.

    ``kind`` is a ``NotificationKind`` value. Topics and the access token are
    never task arguments: they are loaded in the worker.
    """
    try:
        notification_kind = NotificationKind(kind)
    except ValueError:
        logger.error("Notification skipped: unknown kind %r", kind)
        return
    message = NotificationMessage.create(
        kind=notification_kind, title=title, body=body, path=path
    )
    sent = send_to_users(user_ids=user_ids, message=message)
    logger.info("Notification %s: %d of %d user(s) reached", kind, sent, len(user_ids))
