"""Send a test notification to the user's own devices (throttled)."""

from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from django.urls import reverse

from accounts.models import CustomUser
from notifications.exceptions import NoPushDevicesError, TestPushThrottledError
from notifications.services.push_config import ensure_push_configured
from notifications.services.push_message import PushMessage
from notifications.services.subscriptions import count_user_devices
from notifications.tasks import send_push

# One test notification per user per window.
TEST_PUSH_WINDOW_SECONDS = 30


def _throttle_key(user_id: int) -> str:
    return f"notifications:test-push:{user_id}"


def send_test_push(user: CustomUser) -> int:
    """Queue a test push to every device of ``user``; returns the device count."""
    ensure_push_configured()
    devices = count_user_devices(user)
    if devices == 0:
        raise NoPushDevicesError()
    # cache.add is atomic: only the first request in the window gets the key.
    if not cache.add(_throttle_key(user.pk), 1, timeout=TEST_PUSH_WINDOW_SECONDS):
        raise TestPushThrottledError(TEST_PUSH_WINDOW_SECONDS)
    message = PushMessage(
        title="Test notification",
        body="Notifications work on this device.",
        url=reverse("notifications:settings"),
        tag="test-notification",
    )
    transaction.on_commit(
        lambda: send_push.delay(
            [user.pk], message.title, message.body, message.url, message.tag
        )
    )
    return devices
