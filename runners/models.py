"""The user-owned Test Runner (plan: docs/plan/feat-user-test-runners.md).

A runner belongs to one user, authenticates with its own token and holds its
own connection state (moved here from ``Project.agent_*``).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from projects.models import RunnerDeviceState
from runners.types import RunnerId

RUNNER_NAME_MAX_LENGTH = 64
# sha256 hex digest of the plaintext token.
RUNNER_TOKEN_HASH_LENGTH = 64


class TestRunner(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="test_runners",
    )
    # Plain text chosen by the owner; always escaped, never ``|safe``.
    name = models.CharField(max_length=RUNNER_NAME_MAX_LENGTH)
    # sha256 hex of the token: the handshake looks it up by equality, then
    # compares in constant time. Never shown, never logged.
    token_hash = models.CharField(max_length=RUNNER_TOKEN_HASH_LENGTH, unique=True)
    # Fernet ciphertext (``projects.secrets.encrypt_secret``) so the owner can
    # download the ZIP again without minting a new token.
    token_encrypted = models.TextField()

    # Connection state, written by ``runners.services.runner_connection``.
    connected = models.BooleanField(default=False, db_index=True)
    # The owning consumer's channel; "" while disconnected.
    channel_name = models.CharField(max_length=255, blank=True, default="")
    # Handshake ``system_info`` (machine-supplied; escaped, fenced in prompts).
    system_info = models.JSONField(default=dict, blank=True)
    client_version = models.CharField(max_length=32, blank=True, default="")
    capabilities = models.JSONField(default=list, blank=True)
    # The phone or tablet of a phone runner; blank for a desktop runner.
    device_state = models.CharField(
        max_length=16,
        choices=RunnerDeviceState.choices,
        blank=True,
        default=RunnerDeviceState.NONE,
    )
    device_detail = models.CharField(max_length=500, blank=True, default="")
    device_reported_at = models.DateTimeField(null=True, blank=True)
    # When the last connection ended (as ``Project.last_connected_at`` was).
    last_connected_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Soft delete: finished runs keep the row (and their name snapshot).
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Unique per owner, case-insensitively, among non-archived runners:
            # an archived runner frees its name.
            models.UniqueConstraint(
                Lower("name"),
                "owner",
                condition=Q(archived_at__isnull=True),
                name="runner_name_unique_per_owner",
            )
        ]
        indexes = [models.Index(fields=["owner", "archived_at"])]

    def __str__(self) -> str:
        return f"Test Runner {self.name} (#{self.pk})"

    @property
    def runner_id(self) -> RunnerId:
        """The typed id for ``controller_*`` and other runner-keyed services."""
        return RunnerId(self.pk)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None
