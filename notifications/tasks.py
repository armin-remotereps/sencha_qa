from __future__ import annotations

import logging

from celery import shared_task

from notifications.services.push_message import PushMessage
from notifications.services.send_push import send_push_to_users

logger = logging.getLogger(__name__)


@shared_task(
    name="notifications.tasks.send_push",
    queue="upload",
    max_retries=0,
    # Each request has WEBPUSH_REQUEST_TIMEOUT; sends run in parallel.
    soft_time_limit=120,
    time_limit=150,
)
def send_push(user_ids: list[int], title: str, body: str, url: str, tag: str) -> None:
    sent = send_push_to_users(
        user_ids=user_ids,
        message=PushMessage(title=title, body=body, url=url, tag=tag),
    )
    logger.info("send_push: users=%d devices_reached=%d", len(user_ids), sent)
