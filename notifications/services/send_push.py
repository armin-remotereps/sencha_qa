"""Deliver one Web Push message to every device of some users.

Endpoints and keys are never logged; failures are logged by subscription id.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum

from django.conf import settings
from django.utils import timezone
from pywebpush import WebPushException, webpush

from notifications.models import PushSubscription
from notifications.services.endpoint_policy import is_endpoint_allowed
from notifications.services.push_config import push_configured
from notifications.services.push_message import PushMessage

logger = logging.getLogger(__name__)

# Concurrent requests to the push services for one message.
MAX_PARALLEL_SENDS = 8
# The push service says the subscription no longer exists.
GONE_STATUS_CODES = frozenset({404, 410})


class _Outcome(Enum):
    SENT = "sent"
    GONE = "gone"
    FAILED = "failed"


@dataclass(frozen=True)
class _Target:
    subscription_id: int
    endpoint: str
    p256dh: str
    auth: str


def _payload(message: PushMessage) -> str:
    return json.dumps(
        {
            "title": message.title,
            "body": message.body,
            "url": message.url,
            "tag": message.tag,
        }
    )


def _send_one(target: _Target, data: str) -> _Outcome:
    # Re-checked here: the allowlist may have been narrowed since the
    # subscription was saved.
    if not is_endpoint_allowed(target.endpoint):
        logger.warning(
            "Push skipped: subscription %s endpoint host is not allowed",
            target.subscription_id,
        )
        return _Outcome.FAILED
    try:
        webpush(
            subscription_info={
                "endpoint": target.endpoint,
                "keys": {"p256dh": target.p256dh, "auth": target.auth},
            },
            data=data,
            vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
            # A fresh dict per call: webpush writes the endpoint's "aud" into it.
            vapid_claims={"sub": settings.WEBPUSH_VAPID_SUBJECT},
            ttl=settings.WEBPUSH_TTL_SECONDS,
            timeout=settings.WEBPUSH_REQUEST_TIMEOUT,
            headers={"Urgency": "high"},
        )
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in GONE_STATUS_CODES:
            return _Outcome.GONE
        # exc.message can echo the response body; log only the status.
        logger.warning(
            "Push failed: subscription %s status %s", target.subscription_id, status
        )
        return _Outcome.FAILED
    except Exception as exc:  # network errors, malformed stored keys, ...
        logger.warning(
            "Push failed: subscription %s %s",
            target.subscription_id,
            type(exc).__name__,
        )
        return _Outcome.FAILED
    return _Outcome.SENT


def send_push_to_users(*, user_ids: list[int], message: PushMessage) -> int:
    """Push ``message`` to every device of ``user_ids``; returns devices reached.

    Devices whose push service answers 404/410 are deleted. Never raises for a
    single device's failure.
    """
    if not user_ids or not push_configured():
        return 0
    targets = [
        _Target(subscription_id=row_id, endpoint=endpoint, p256dh=p256dh, auth=auth)
        for row_id, endpoint, p256dh, auth in PushSubscription.objects.filter(
            user_id__in=user_ids
        ).values_list("id", "endpoint", "p256dh", "auth")
    ]
    if not targets:
        return 0

    data = _payload(message)
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_SENDS, len(targets))) as pool:
        outcomes = list(pool.map(lambda target: _send_one(target, data), targets))

    sent_ids = [
        t.subscription_id for t, o in zip(targets, outcomes) if o is _Outcome.SENT
    ]
    gone_ids = [
        t.subscription_id for t, o in zip(targets, outcomes) if o is _Outcome.GONE
    ]
    if gone_ids:
        PushSubscription.objects.filter(pk__in=gone_ids).delete()
        logger.info("Push: removed %d expired subscription(s)", len(gone_ids))
    if sent_ids:
        PushSubscription.objects.filter(pk__in=sent_ids).update(
            last_success_at=timezone.now()
        )
    return len(sent_ids)
