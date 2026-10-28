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
    notify_night_digest = models.BooleanField(default=True)
    notify_run_finished = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Notification preference (user {self.user_id})"


# Reply previews are model-written plain text, shown escaped.
MISSED_REPLY_PREVIEW_MAX_LENGTH = 140


class MissedFailure(models.Model):
    """A run case the agent judged Failed, in one member's attention inbox.

    One row per (user, case): a new failure of the same case re-opens it
    (``occurred_at`` moved on, ``dismissed_at`` cleared). Listed only while the
    case is still Failed; the user dismisses it by hand.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    test_run_test_case = models.ForeignKey(
        "projects.TestRunTestCase", on_delete=models.CASCADE, related_name="+"
    )
    occurred_at = models.DateTimeField()
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "test_run_test_case"], name="missed_failure_user_case"
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "-occurred_at"],
                condition=models.Q(dismissed_at__isnull=True),
                name="missed_failure_open_idx",
            )
        ]

    def __str__(self) -> str:
        return f"Missed failure #{self.pk} (user {self.user_id})"


class MissedReplyOutcome(models.TextChoices):
    """How the setup turn the user missed ended."""

    REPLY = "reply", "Reply"
    PAUSED = "paused", "Paused"
    FAILED = "failed", "Failed"


class MissedReply(models.Model):
    """A setup turn that ended while its sender was away, in their inbox.

    One row per (user, conversation): a newer missed turn re-opens it.
    Hidden once the thread is archived; the user dismisses it by hand.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    conversation = models.ForeignKey(
        "machine_setup.SetupConversation", on_delete=models.CASCADE, related_name="+"
    )
    outcome = models.CharField(max_length=16, choices=MissedReplyOutcome.choices)
    preview = models.CharField(
        max_length=MISSED_REPLY_PREVIEW_MAX_LENGTH, blank=True, default=""
    )
    occurred_at = models.DateTimeField()
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "conversation"], name="missed_reply_user_conversation"
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "-occurred_at"],
                condition=models.Q(dismissed_at__isnull=True),
                name="missed_reply_open_idx",
            )
        ]

    def __str__(self) -> str:
        return f"Missed reply #{self.pk} (user {self.user_id})"
