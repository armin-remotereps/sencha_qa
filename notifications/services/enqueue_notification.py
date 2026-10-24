"""Hand one notification to the ``notify`` queue."""

from __future__ import annotations

import logging

from notifications.services.notification_message import NotificationMessage
from notifications.tasks import send_notification

logger = logging.getLogger(__name__)


def enqueue_notification(user_ids: list[int], message: NotificationMessage) -> None:
    """Queue ``message`` for ``user_ids``; logs and never raises.

    Only user ids go into the task arguments: topics are loaded in the worker.
    Call it from ``transaction.on_commit`` so nothing is sent for a rollback.
    """
    try:
        send_notification.delay(
            user_ids, message.kind.value, message.title, message.body, message.path
        )
    except Exception:
        logger.exception("Could not enqueue the %s notification", message.kind.value)
