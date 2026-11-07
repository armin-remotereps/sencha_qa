"""Staging rows of the MCP gateway (internal, never rendered).

While an MCP tester performs a claimed Manual run case, its screenshots and
step marks live here until the hand-in turns them into the ordinary manual
records (``ManualCaseSubmission``, ``TestRunScreenshot``). Rows are scoped to
one claim (``pivot`` + ``claim_token``) and deleted at hand-in and release,
and by the hourly sweep once the claim no longer matches (never while it is
live).
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from controller_client.protocol import MANUAL_SHOT_KEY_MAX_CHARS
from projects.models import MANUAL_CLAIM_TOKEN_LENGTH, TestRunTestCase


class McpStagedShot(models.Model):
    """One screenshot the MCP tester sent for a claimed case (validated)."""

    pivot = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="+"
    )
    claim_token = models.CharField(max_length=MANUAL_CLAIM_TOKEN_LENGTH)
    # Server-minted, the protocol's screenshot key format; the tool's shot_id.
    shot_key = models.CharField(max_length=MANUAL_SHOT_KEY_MAX_CHARS)
    # ``ManualShotMime`` value.
    mime = models.CharField(max_length=16)
    size = models.PositiveIntegerField()
    # Lowercase hex of ``data``.
    sha256 = models.CharField(max_length=64)
    data = models.BinaryField()
    # Null until a mark binds it to a step (or it is the case screenshot).
    step_num = models.PositiveSmallIntegerField(null=True, blank=True)
    # ``ManualShotKind`` of a bound shot ("pass" / "fail"); blank while unbound.
    kind = models.CharField(max_length=8, blank=True, default="")
    bound_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pivot", "shot_key"], name="mcp_staged_shot_key_uniq"
            )
        ]
        indexes = [
            models.Index(
                fields=["pivot", "claim_token"], name="mcp_staged_shot_claim_idx"
            )
        ]

    def __str__(self) -> str:
        return f"McpStagedShot #{self.pk}"


class McpStepMark(models.Model):
    """The MCP tester's mark of one step of a claimed case (Pass / Fail).

    The hand-in is built from these rows and the bound shots; the advisory
    ``ManualCaseProgress`` row is only kept in step for the web card.
    """

    pivot = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="+"
    )
    claim_token = models.CharField(max_length=MANUAL_CLAIM_TOKEN_LENGTH)
    step_num = models.PositiveSmallIntegerField()
    # ``ManualStepOutcome`` value.
    outcome = models.CharField(max_length=8)
    note = models.TextField(blank=True, default="")
    # Time spent on the step, capped at MANUAL_STEP_MAX_SECONDS.
    seconds = models.PositiveIntegerField(default=0)
    marked_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pivot", "claim_token", "step_num"],
                name="mcp_step_mark_step_uniq",
            )
        ]

    def __str__(self) -> str:
        return f"McpStepMark #{self.pk}"
