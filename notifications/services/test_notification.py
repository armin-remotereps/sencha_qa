"""Send a test notification to the user's own ntfy topic (throttled)."""

from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from django.urls import reverse

from accounts.models import CustomUser
from notifications.exceptions import (
    NotificationsOffError,
    TestNotificationThrottledError,
)
from notifications.models import NotificationPreference
from notifications.services.notification_message import (
    NotificationKind,
    NotificationMessage,
)
from notifications.services.ntfy_config import ensure_ntfy_configured
from notifications.tasks import send_notification

# One test notification per user per window.
TEST_NOTIFICATION_WINDOW_SECONDS = 30


def _throttle_key(user_id: int) -> str:
    return f"notifications:test-notification:{user_id}"


def send_test_notification(user: CustomUser) -> None:
    """Queue a test notification to ``user``'s own topic.

    Raises ``NotificationsNotConfiguredError``, ``NotificationsOffError`` when
    the user's notifications are not on, or ``TestNotificationThrottledError``.
    """
    ensure_ntfy_configured()
    if not NotificationPreference.objects.filter(user=user, enabled=True).exists():
        raise NotificationsOffError()
    # cache.add is atomic: only the first request in the window gets the key.
    if not cache.add(
        _throttle_key(user.pk), 1, timeout=TEST_NOTIFICATION_WINDOW_SECONDS
    ):
        raise TestNotificationThrottledError(TEST_NOTIFICATION_WINDOW_SECONDS)
    message = NotificationMessage.create(
        kind=NotificationKind.TEST,
        title="Test notification",
        body="Notifications from this server reach you.",
        path=reverse("notifications:settings"),
    )
    # The task loads the topic itself: secrets never go in task arguments.
    transaction.on_commit(
        lambda: send_notification.delay(
            [user.pk],
            message.kind.value,
            message.title,
            message.body,
            message.path,
        )
    )
