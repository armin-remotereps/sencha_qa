from __future__ import annotations

from django.conf import settings
from django.db import models

PUSH_ENDPOINT_MAX_LENGTH = 1000


class PushSubscription(models.Model):
    """One browser or device that accepts Web Push for a user.

    ``endpoint``, ``p256dh`` and ``auth`` are secrets of the device: they are
    never rendered or logged.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.URLField(max_length=PUSH_ENDPOINT_MAX_LENGTH, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    # Shown to the user so they can tell their devices apart.
    user_agent = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Push subscription #{self.pk} (user {self.user_id})"
