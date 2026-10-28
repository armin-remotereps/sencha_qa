from __future__ import annotations

from django.conf import settings
from django.db import models

PRESET_NAME_MAX_LENGTH = 60
PRESET_QUERY_MAX_LENGTH = 1000
# At most this many presets per user (checked in the service).
PRESET_LIMIT = 20


class FailedCaseFilterPreset(models.Model):
    """A user's named set of Failures page filters.

    ``query`` is the encoded query string of the cleaned filters (never page
    or per_page); loading a preset links to ``/failures/?<query>``, where the
    filter form validates it again.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="failed_case_filter_presets",
    )
    name = models.CharField(max_length=PRESET_NAME_MAX_LENGTH)
    query = models.CharField(max_length=PRESET_QUERY_MAX_LENGTH)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name"], name="failed_case_preset_user_name"
            )
        ]

    def __str__(self) -> str:
        return f"Filter preset #{self.pk} (user {self.user_id})"
