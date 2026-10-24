"""Publish one notification to the ntfy topics of some users.

Each message is a JSON ``POST`` to the ntfy server root, so the topic travels
only in the request body and never in a URL that httpx, a proxy or an access
log would record. Topics and the access token are never logged: failures are
logged by user id with the HTTP status or the exception type name only.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum

import httpx
from django.conf import settings

from notifications.models import NotificationPreference
from notifications.services.notification_message import (
    NotificationMessage,
    preference_flag,
)
from notifications.services.ntfy_config import ntfy_configured, ntfy_server_url
from notifications.services.site_url import absolute_url

logger = logging.getLogger(__name__)

# Concurrent requests to the ntfy server for one message.
MAX_PARALLEL_SENDS = 4
# ntfy's answer when the visitor's request or message budget is used up.
RATE_LIMITED_STATUS = 429


class _Outcome(Enum):
    SENT = "sent"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class _Target:
    user_id: int
    topic: str


def _targets(user_ids: list[int], message: NotificationMessage) -> list[_Target]:
    """One query: the users' topics, re-checking they still want this kind."""
    queryset = NotificationPreference.objects.filter(user_id__in=user_ids, enabled=True)
    flag = preference_flag(message.kind)
    if flag is not None:
        queryset = queryset.filter(**{flag: True})
    return [
        _Target(user_id=user_id, topic=topic)
        for user_id, topic in queryset.values_list("user_id", "topic")
    ]


def _base_payload(message: NotificationMessage) -> dict[str, object]:
    delivery = message.delivery
    payload: dict[str, object] = {
        "title": message.title,
        "message": message.body,
        "priority": delivery.priority,
        "tags": list(delivery.tags),
    }
    click = absolute_url(message.path)
    if click is not None:
        payload["click"] = click
    return payload


def _client() -> httpx.Client:
    headers: dict[str, str] = {}
    token = str(settings.NTFY_ACCESS_TOKEN).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=ntfy_server_url(),
        headers=headers,
        timeout=float(settings.NTFY_REQUEST_TIMEOUT),
        limits=httpx.Limits(max_connections=MAX_PARALLEL_SENDS),
        follow_redirects=False,
    )


def _send_one(
    client: httpx.Client,
    target: _Target,
    base_payload: dict[str, object],
    stop_batch: threading.Event,
) -> _Outcome:
    # A 429 or a transport error (unreachable, slow or dropping server) stops
    # the rest of the batch: no retry storm, and a bad server costs at most one
    # timeout per worker, not one per user.
    if stop_batch.is_set():
        return _Outcome.SKIPPED
    try:
        response = client.post("/", json={**base_payload, "topic": target.topic})
    except httpx.TransportError as exc:
        stop_batch.set()
        logger.warning(
            "ntfy server unreachable or slow: user %s %s",
            target.user_id,
            type(exc).__name__,
        )
        return _Outcome.FAILED
    except httpx.HTTPError as exc:
        # The exception text can carry the request; log only its type.
        logger.warning(
            "ntfy publish failed: user %s %s", target.user_id, type(exc).__name__
        )
        return _Outcome.FAILED
    if response.is_success:
        return _Outcome.SENT
    if response.status_code == RATE_LIMITED_STATUS:
        stop_batch.set()
        return _Outcome.RATE_LIMITED
    # The response body can echo the request (and so the topic); log the status.
    logger.warning(
        "ntfy publish failed: user %s status %s",
        target.user_id,
        response.status_code,
    )
    return _Outcome.FAILED


def send_to_users(*, user_ids: list[int], message: NotificationMessage) -> int:
    """Publish ``message`` to each user's topic; returns the topics accepted.

    Users whose notifications are off, or who no longer opt into
    ``message.kind``, are skipped. Never raises for one user's failure, never
    retries, and nothing is deleted on failure (ntfy has no "gone" signal).
    """
    if not user_ids or not ntfy_configured():
        return 0
    targets = _targets(user_ids, message)
    if not targets:
        return 0

    base_payload = _base_payload(message)
    stop_batch = threading.Event()
    with (
        _client() as client,
        ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_SENDS, len(targets))) as pool,
    ):
        outcomes = list(
            pool.map(
                lambda target: _send_one(client, target, base_payload, stop_batch),
                targets,
            )
        )

    sent = outcomes.count(_Outcome.SENT)
    if stop_batch.is_set():
        logger.warning(
            "ntfy batch stopped early for the %s notification: %d sent, "
            "%d rate limited, %d skipped",
            message.kind,
            sent,
            outcomes.count(_Outcome.RATE_LIMITED),
            outcomes.count(_Outcome.SKIPPED),
        )
    return sent
