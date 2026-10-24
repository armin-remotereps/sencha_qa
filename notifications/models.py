from __future__ import annotations

from django.conf import settings
from django.db import models

# ntfy topic names are at most 64 characters of [-_A-Za-z0-9].
NTFY_TOPIC_MAX_LENGTH = 64


class NotificationPreference(models.Model):
    """A user's ntfy subscription and the kinds of notifications they want.

    The row is created the first time the user turns notifications on; turning
    them off keeps it, so turning them back on keeps the same topic.

    ``topic`` is a secret (on public ntfy servers anyone who knows it can read
    and publish): it is shown only to its owner and never logged, put in task
    arguments or shown in the admin.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )
    topic = models.CharField(max_length=NTFY_TOPIC_MAX_LENGTH, unique=True)
    enabled = models.BooleanField(default=True)
    notify_need_help = models.BooleanField(default=True)
    notify_failed = models.BooleanField(default=True)
    notify_chat_reply = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Notification preference (user {self.user_id})"
