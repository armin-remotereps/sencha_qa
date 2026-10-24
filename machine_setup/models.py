from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from projects.models import CaseHelpRequest, Project


class SetupTurnStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    STOPPED = "stopped", "Stopped"
    FAILED = "failed", "Failed"


LIVE_TURN_STATUSES = (SetupTurnStatus.QUEUED, SetupTurnStatus.RUNNING)


class SetupTurnEndReason(models.TextChoices):
    ANSWERED = "answered", "Answered"
    ITERATION_LIMIT = "iteration_limit", "Step limit reached"
    TIME_LIMIT = "time_limit", "Time limit reached"
    STOPPED_BY_USER = "stopped_by_user", "Stopped"
    RESET = "reset", "Chat reset"
    RUNNER_DISCONNECTED = "runner_disconnected", "Test Runner disconnected"
    VISUAL_ENGINE = "visual_engine", "Visual engine unavailable"
    AI_SERVICE = "ai_service", "AI service error"
    TRANSCRIPT_INVALID = "transcript_invalid", "Chat history unusable"
    WORKER_LOST = "worker_lost", "Worker lost"
    INTERNAL_ERROR = "internal_error", "Internal error"


class SetupMessageKind(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"
    TOOL = "tool", "Tool"
    NOTICE = "notice", "Notice"


class SetupToolStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class SetupSubtaskResult(models.TextChoices):
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    NEEDS_USER = "needs_user", "Needs you"
    UNCLEAR = "unclear", "Unclear"
    INTERRUPTED = "interrupted", "Interrupted"


class SetupConversationKind(models.TextChoices):
    CHAT = "chat", "Chat"
    # Opened by the server for a Night-mode run case that needs help.
    AUTO_FIX = "auto_fix", "Auto-fix"


class SetupFixResult(models.TextChoices):
    """What the setup agent reported through ``report_case_fix``; blank = no report."""

    NONE = "", "No report"
    FIXED = "fixed", "Fixed"
    NOT_FIXED = "not_fixed", "Not fixed"


# Cap on the setup agent's ``report_case_fix`` summary.
SETUP_FIX_SUMMARY_MAX_LENGTH = 2000


class SetupConversation(models.Model):
    """A machine setup thread: the main chat (one active per project) or an auto-fix."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="setup_conversations"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # agents/transcript.py shape, without the system prompt (rebuilt every turn).
    llm_transcript = models.JSONField(default=dict, blank=True)
    kind = models.CharField(
        max_length=16,
        choices=SetupConversationKind.choices,
        default=SetupConversationKind.CHAT,
    )
    # Auto-fix threads only: the help request the thread was opened for.
    help_request = models.ForeignKey(
        CaseHelpRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="setup_conversations",
    )
    # Auto-fix threads only: the agent's last ``report_case_fix`` call.
    fix_result = models.CharField(
        max_length=16, choices=SetupFixResult.choices, blank=True, default=""
    )
    fix_summary = models.TextField(
        max_length=SETUP_FIX_SUMMARY_MAX_LENGTH, blank=True, default=""
    )

    class Meta:
        constraints = [
            # One main chat per project; auto-fix threads are not limited.
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(archived_at__isnull=True, kind="chat"),
                name="setup_one_active_conversation",
            )
        ]

    def __str__(self) -> str:
        return f"Setup conversation #{self.pk} (project {self.project_id})"


class SetupTurn(models.Model):
    """One agent turn, started by one user message."""

    conversation = models.ForeignKey(
        SetupConversation, on_delete=models.CASCADE, related_name="turns"
    )
    # Denormalized so "is any turn live for this project" spans conversations.
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="setup_turns"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    status = models.CharField(
        max_length=20, choices=SetupTurnStatus.choices, default=SetupTurnStatus.QUEUED
    )
    ended_reason = models.CharField(
        max_length=30, choices=SetupTurnEndReason.choices, blank=True, default=""
    )
    # User-safe text shown on the page.
    error_message = models.TextField(blank=True, default="")
    stop_requested = models.BooleanField(default=False)
    stop_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    iterations = models.PositiveIntegerField(default=0)
    # The one turn the server starts for an auto-fix thread; later turns a
    # person starts in that thread are ordinary turns.
    auto_fix = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Database backstop for "one agent drives a machine at a time".
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(status__in=["queued", "running"]),
                name="setup_one_live_turn_per_project",
            )
        ]
        indexes = [models.Index(fields=["project", "status"])]

    def __str__(self) -> str:
        return f"Setup turn #{self.pk} ({self.status})"


class SetupMessage(models.Model):
    """A row of the displayed chat; separate from the replayable LLM transcript."""

    conversation = models.ForeignKey(
        SetupConversation, on_delete=models.CASCADE, related_name="messages"
    )
    turn = models.ForeignKey(
        SetupTurn,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
    )
    kind = models.CharField(max_length=20, choices=SetupMessageKind.choices)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    text = models.TextField(blank=True, default="")
    # Server-built from reverse(), never from model text.
    link_url = models.CharField(max_length=500, blank=True, default="")
    tool_name = models.CharField(max_length=100, blank=True, default="")
    tool_call_id = models.CharField(max_length=255, blank=True, default="")
    tool_arguments = models.JSONField(default=dict, blank=True)
    tool_status = models.CharField(
        max_length=20, choices=SetupToolStatus.choices, blank=True, default=""
    )
    # Set on rows a sub-agent wrote: the main agent's run_setup_task row.
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        # Covered by the (parent, id) index below.
        db_index=False,
    )
    # Set on run_setup_task rows only: how the sub-task ended.
    subtask_result = models.CharField(
        max_length=20, choices=SetupSubtaskResult.choices, blank=True, default=""
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["conversation", "id"]),
            models.Index(fields=["parent", "id"], name="setup_msg_parent_id"),
            models.Index(
                fields=["conversation", "id"],
                condition=Q(parent__isnull=True),
                name="setup_msg_top_level",
            ),
        ]

    def __str__(self) -> str:
        return f"Setup message #{self.pk} ({self.kind})"


def _setup_screenshot_upload_path(instance: SetupScreenshot, filename: str) -> str:
    return f"machine_setup/conv_{instance.message.conversation_id}/{filename}"


class SetupScreenshot(models.Model):
    message = models.ForeignKey(
        SetupMessage, on_delete=models.CASCADE, related_name="screenshots"
    )
    image = models.ImageField(upload_to=_setup_screenshot_upload_path)
    tool_name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"Setup screenshot #{self.pk} — {self.tool_name}"
