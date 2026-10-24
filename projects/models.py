from __future__ import annotations

import secrets
from dataclasses import dataclass

from django.conf import settings
from django.db import models


def _default_api_key() -> str:
    return secrets.token_urlsafe(32)


class TestCaseType(models.TextChoices):
    ACCEPTANCE = "Acceptance", "Acceptance"
    ACCESSIBILITY = "Accessibility", "Accessibility"
    AUTOMATED = "Automated", "Automated"
    COMPATIBILITY = "Compatibility", "Compatibility"
    DESTRUCTIVE = "Destructive", "Destructive"
    FUNCTIONAL = "Functional", "Functional"
    OTHER = "Other", "Other"
    PERFORMANCE = "Performance", "Performance"
    REGRESSION = "Regression", "Regression"
    SECURITY = "Security", "Security"
    SMOKE_SANITY = "Smoke & Sanity", "Smoke & Sanity"
    USABILITY = "Usability", "Usability"


class TestCasePriority(models.TextChoices):
    DONT_TEST = "1 - Don't Test", "1 - Don't Test"
    TEST_IF_TIME_LOW = "2 - Test If Time", "2 - Test If Time"
    TEST_IF_TIME_MID = "3 - Test If Time", "3 - Test If Time"
    MUST_TEST = "4 - Must Test", "4 - Must Test"
    MUST_TEST_HIGH = "5 - Must Test", "5 - Must Test"


class UploadStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class UploadSource(models.TextChoices):
    XML = "xml", "TestRail XML export"
    TESTRAIL_API = "testrail_api", "TestRail account"


class TestRunStatus(models.TextChoices):
    WAITING = "waiting", "Waiting"
    STARTED = "started", "Started"
    DONE = "done", "Done"
    CANCELLED = "cancelled", "Cancelled"
    # Asleep: a case needs help; nothing in the run executes until it is resolved.
    PAUSED = "paused", "Needs help"


# A run in either status holds the project's machine: no other run may start.
ACTIVE_RUN_STATUSES: tuple[str, ...] = (TestRunStatus.STARTED, TestRunStatus.PAUSED)


class RunHelpMode(models.TextChoices):
    """What happens when a case of the run needs help."""

    ATTENDED = "attended", "Attended"
    NIGHT = "night", "Night (auto-fix first)"


class TestRunTestCaseStatus(models.TextChoices):
    CREATED = "created", "Created"
    IN_PROGRESS = "in_progress", "In Progress"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    BLOCKED = "blocked", "Blocked"
    # Stopped without a verdict at a failing step; waits for a person (or the
    # auto-fix agent) with the checkpoint and the machine state kept.
    NEED_HELP = "need_help", "Need help"


class BlockedReason(models.TextChoices):
    """Why a case could not be executed or judged; never a verdict on the app."""

    RUNNER_DISCONNECTED = "runner_disconnected", "Test Runner disconnected"
    RUNNER_UNRESPONSIVE = "runner_unresponsive", "Test Runner stopped responding"
    VISUAL_ENGINE = "visual_engine", "Visual engine unavailable"
    AI_SERVICE = "ai_service", "AI service error"
    TIME_LIMIT = "time_limit", "Case time limit reached"
    BROWSER_UNAVAILABLE = (
        "browser_unavailable",
        "Browser unavailable on the test machine",
    )
    RUNNER_OUTDATED = "runner_outdated", "Test Runner needs an update"
    RESUME_STATE_LOST = (
        "resume_state_lost",
        "Could not restore the state to resume",
    )
    INTERNAL_ERROR = "internal_error", "Internal error"
    HELP_SKIPPED = "help_skipped", "Skipped while waiting for help"


class ApplicationPlatform(models.TextChoices):
    WEB = "web", "Web application"
    WINDOWS = "windows", "Windows desktop"
    BOTH = "both", "Both"


class TestRailPushStatus(models.TextChoices):
    NOT_PUSHED = "not_pushed", "Not pushed"
    PUSHING = "pushing", "Pushing"
    PUSHED = "pushed", "Pushed"
    FAILED = "failed", "Failed"


class CheckedPushSkipReason(models.TextChoices):
    """Why a checked case's result could not be sent to its source TestRail run."""

    NOT_IN_RUN = "not_in_run", "Not in the source run"
    RUN_CLOSED = "run_closed", "Source run closed"
    RUN_MISSING = "run_missing", "Source run not found"
    NO_CASE_ID = "no_case_id", "No TestRail case ID"


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True, db_index=True)

    def __str__(self) -> str:
        return self.name


# Cap on the application context notes, for every writer (form and agent tool).
PROJECT_PROMPT_MAX_LENGTH = 12000
CONTEXT_CHANGE_SUMMARY_MAX_LENGTH = 500


class ContextWriteSource(models.TextChoices):
    USER = "user", "You"
    REFINER = "refiner", "AI refine"
    SETUP_AGENT = "setup_agent", "Machine setup agent"
    RESTORE = "restore", "Restore"


class Project(models.Model):
    name = models.CharField(max_length=255)
    tags = models.ManyToManyField(Tag, blank=True, related_name="projects")
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="projects"
    )
    archived = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    api_key = models.CharField(
        max_length=64, unique=True, db_index=True, default=_default_api_key
    )
    agent_connected = models.BooleanField(default=False, db_index=True)
    # Channels channel_name of the consumer that owns the controller connection.
    # A late disconnect of an older socket only releases the project if it still
    # owns it, so a reconnect is never wiped by its predecessor's cleanup.
    agent_channel_name = models.CharField(max_length=255, blank=True, default="")
    agent_system_info = models.JSONField(default=dict, blank=True)
    agent_client_version = models.CharField(max_length=32, blank=True, default="")
    agent_capabilities = models.JSONField(default=list, blank=True)
    last_connected_at = models.DateTimeField(null=True, blank=True)
    project_prompt = models.TextField(blank=True, default="")
    # Bumped on every write; the setup agent's write is conditional on it.
    project_prompt_revision = models.PositiveIntegerField(default=0)
    # Provenance of the current notes, copied into the history on replacement.
    project_prompt_source = models.CharField(
        max_length=20, choices=ContextWriteSource.choices, blank=True, default=""
    )
    project_prompt_written_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    project_prompt_written_at = models.DateTimeField(null=True, blank=True)
    project_prompt_change_summary = models.CharField(
        max_length=CONTEXT_CHANGE_SUMMARY_MAX_LENGTH, blank=True, default=""
    )
    application_url = models.URLField(max_length=500, blank=True, default="")
    application_platform = models.CharField(
        max_length=16,
        choices=ApplicationPlatform.choices,
        blank=True,
        default="",
    )
    testrail_url = models.URLField(max_length=500, blank=True, default="")
    testrail_email = models.EmailField(blank=True, default="")
    testrail_api_key_encrypted = models.TextField(blank=True, default="")

    def __str__(self) -> str:
        return self.name

    @property
    def has_application_context(self) -> bool:
        """Gates the "add application context" prompts on the dashboard and overview."""
        return bool(
            self.application_url
            or self.application_platform
            or self.project_prompt.strip()
        )

    @property
    def has_testrail_settings(self) -> bool:
        """True when URL, email and an encrypted API key are all stored."""
        return bool(
            self.testrail_url
            and self.testrail_email
            and self.testrail_api_key_encrypted
        )


class ApplicationContextVersion(models.Model):
    """A replaced value of ``Project.project_prompt``, with who wrote it."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="context_versions"
    )
    # The revision this text had while it was current.
    revision = models.PositiveIntegerField()
    text = models.TextField(blank=True, default="")
    written_by_source = models.CharField(
        max_length=20, choices=ContextWriteSource.choices, blank=True, default=""
    )
    # The author; for the setup agent, the user whose turn wrote it.
    written_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    written_at = models.DateTimeField(null=True, blank=True)
    change_summary = models.CharField(
        max_length=CONTEXT_CHANGE_SUMMARY_MAX_LENGTH, blank=True, default=""
    )
    replaced_at = models.DateTimeField()

    class Meta:
        ordering = ["-revision"]
        indexes = [models.Index(fields=["project", "-revision"])]

    def __str__(self) -> str:
        return f"Context revision {self.revision} (project {self.project_id})"


class TestCaseUpload(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="uploads"
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="uploads"
    )
    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to="uploads/testrail_xml/", blank=True)
    source = models.CharField(
        max_length=20,
        choices=UploadSource.choices,
        default=UploadSource.XML,
        db_index=True,
    )
    testrail_project_id = models.PositiveIntegerField(null=True, blank=True)
    testrail_suite_id = models.PositiveIntegerField(null=True, blank=True)
    testrail_run_id = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=UploadStatus.choices,
        default=UploadStatus.PENDING,
        db_index=True,
    )
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    total_cases = models.PositiveIntegerField(default=0)
    processed_cases = models.PositiveIntegerField(default=0)
    updated_cases = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.original_filename

    @property
    def is_testrail_api(self) -> bool:
        return self.source == UploadSource.TESTRAIL_API

    @property
    def is_testrail_run_import(self) -> bool:
        return self.testrail_run_id is not None

    @property
    def created_cases(self) -> int:
        """Cases this import created (processed counts created + updated)."""
        return max(self.processed_cases - self.updated_cases, 0)


@dataclass
class TestCaseData:
    title: str
    testrail_id: str = ""
    template: str = "Test Case"
    type: str = ""
    priority: str = ""
    estimate: str = ""
    references: str = ""
    preconditions: str = ""
    steps: str = ""
    expected: str = ""


class TestCase(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="test_cases"
    )
    upload = models.ForeignKey(
        TestCaseUpload,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="test_cases",
    )
    testrail_id = models.CharField(max_length=50, blank=True, default="")
    # TestRail suite order, written by imports only (never by TestCaseData).
    testrail_suite_id = models.PositiveIntegerField(null=True, blank=True)
    testrail_position = models.PositiveIntegerField(null=True, blank=True)
    # The TestRail run this case was last imported from with a run link.
    testrail_source_run_id = models.PositiveIntegerField(null=True, blank=True)
    title = models.CharField(max_length=500)
    template = models.CharField(max_length=100, default="Test Case")
    type = models.CharField(
        max_length=50,
        choices=TestCaseType.choices,
        default=TestCaseType.FUNCTIONAL,
    )
    priority = models.CharField(
        max_length=50,
        choices=TestCasePriority.choices,
        default=TestCasePriority.MUST_TEST_HIGH,
    )
    estimate = models.CharField(max_length=50, blank=True, default="")
    references = models.CharField(max_length=500, blank=True, default="")
    preconditions = models.TextField(blank=True, default="")
    steps = models.TextField(blank=True, default="")
    expected = models.TextField(blank=True, default="")
    is_converted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["project", "testrail_id"], name="testcase_project_testrail_idx"
            )
        ]

    def __str__(self) -> str:
        return self.title


class TestRun(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="test_runs"
    )
    test_cases: models.ManyToManyField[TestCase, "TestRunTestCase"] = (
        models.ManyToManyField(
            TestCase, through="TestRunTestCase", related_name="test_runs"
        )
    )
    status = models.CharField(
        max_length=20,
        choices=TestRunStatus.choices,
        default=TestRunStatus.WAITING,
        db_index=True,
    )
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    # Bumped by every Start and Resume; chain tasks carry the value they were
    # queued with and stand down (no execution, no writes) once it moves on.
    execution_generation = models.PositiveIntegerField(default=0)
    # Chosen at Start; kept by Resume and Re-run in place unless changed there.
    help_mode = models.CharField(
        max_length=16, choices=RunHelpMode.choices, default=RunHelpMode.ATTENDED
    )
    name = models.CharField(max_length=255, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    testrail_project_id = models.PositiveIntegerField(null=True, blank=True)
    testrail_suite_id = models.PositiveIntegerField(null=True, blank=True)
    testrail_run_id = models.PositiveIntegerField(null=True, blank=True)
    testrail_push_status = models.CharField(
        max_length=20,
        choices=TestRailPushStatus.choices,
        default=TestRailPushStatus.NOT_PUSHED,
    )
    testrail_pushed_at = models.DateTimeField(null=True, blank=True)
    testrail_push_error = models.TextField(blank=True, default="")
    testrail_push_summary = models.CharField(max_length=255, blank=True, default="")
    # "Send checked to TestRail": kept apart from the whole-run push fields above.
    testrail_source_push_status = models.CharField(
        max_length=20,
        choices=TestRailPushStatus.choices,
        default=TestRailPushStatus.NOT_PUSHED,
    )
    testrail_source_push_started_at = models.DateTimeField(null=True, blank=True)
    testrail_source_pushed_at = models.DateTimeField(null=True, blank=True)
    testrail_source_push_error = models.TextField(blank=True, default="")
    testrail_source_push_summary = models.CharField(
        max_length=255, blank=True, default=""
    )

    def __str__(self) -> str:
        return f"TestRun #{self.pk} — {self.project.name}"

    @property
    def display_name(self) -> str:
        """The run's name, falling back to a stable "Run #N" label."""
        return self.name or f"Run #{self.pk}"

    @property
    def has_testrail_target(self) -> bool:
        """True once both the TestRail project and suite have been chosen."""
        return (
            self.testrail_project_id is not None and self.testrail_suite_id is not None
        )


class TestRunTestCase(models.Model):
    test_run = models.ForeignKey(
        TestRun, on_delete=models.CASCADE, related_name="pivot_entries"
    )
    test_case = models.ForeignKey(
        TestCase, on_delete=models.CASCADE, related_name="run_entries"
    )
    status = models.CharField(
        max_length=20,
        choices=TestRunTestCaseStatus.choices,
        default=TestRunTestCaseStatus.CREATED,
        db_index=True,
    )
    result = models.TextField(blank=True, default="")
    # Set only when status is BLOCKED.
    blocked_reason = models.CharField(
        max_length=32, choices=BlockedReason.choices, blank=True, default=""
    )
    logs = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    testrail_result_id = models.PositiveIntegerField(null=True, blank=True)
    testrail_pushed_status_id = models.PositiveSmallIntegerField(null=True, blank=True)
    testrail_pushed_comment_hash = models.CharField(
        max_length=64, blank=True, default=""
    )
    testrail_pushed_at = models.DateTimeField(null=True, blank=True)
    # Snapshot of test_case.testrail_source_run_id taken when the pivot is created.
    testrail_source_run_id = models.PositiveIntegerField(null=True, blank=True)
    # Null means no human has checked the verdict.
    checked_at = models.DateTimeField(null=True, blank=True)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Set once the result is added to the source run; the dedupe key.
    testrail_source_result_id = models.PositiveIntegerField(null=True, blank=True)
    testrail_source_sent_at = models.DateTimeField(null=True, blank=True)
    # Screenshots already attached to that result; a re-send resumes from here.
    testrail_source_attachment_count = models.PositiveSmallIntegerField(default=0)
    # Set when a send could not deliver this case; it is left out of later
    # sends until unchecked (or the run is reset).
    testrail_source_skip_reason = models.CharField(
        max_length=32, choices=CheckedPushSkipReason.choices, blank=True, default=""
    )
    # Serialized orchestrator progress (plan, attempts, next step, browser) so a
    # Blocked or Cancelled case can resume in place; {} means plan from scratch.
    execution_checkpoint = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("test_run", "test_case")

    def __str__(self) -> str:
        return f"TRTC #{self.pk} — {self.test_case.title[:50]}"


class CaseRestartKind(models.TextChoices):
    RESUME = "resume", "Resumed"
    RERUN = "rerun", "Re-run"


class TestRunCaseResume(models.Model):
    """Snapshot of a case's outcome taken each time it is resumed or re-run in place."""

    test_run_test_case = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="resumes"
    )
    resumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(
        max_length=10, choices=CaseRestartKind.choices, default=CaseRestartKind.RESUME
    )
    # Step the case resumed at; null means it ran from the start (no checkpoint,
    # or a re-run).
    from_step = models.PositiveSmallIntegerField(null=True, blank=True)
    previous_status = models.CharField(
        max_length=20, choices=TestRunTestCaseStatus.choices
    )
    previous_blocked_reason = models.CharField(
        max_length=32, choices=BlockedReason.choices, blank=True, default=""
    )
    previous_result = models.TextField(blank=True, default="")
    previous_finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    @property
    def is_rerun(self) -> bool:
        return self.kind == CaseRestartKind.RERUN

    def __str__(self) -> str:
        return f"Resume #{self.pk} of TRTC #{self.test_run_test_case_id}"


# Caps on the model-written and human-written help texts.
HELP_REQUEST_TEXT_MAX_LENGTH = 1000
HELP_NOTE_MAX_LENGTH = 2000


class HelpAutoFixState(models.TextChoices):
    """Progress of the Night-mode auto-fix of one help request."""

    NONE = "", "None"
    RUNNING = "running", "Running"
    FIXED = "fixed", "Fixed"
    NOT_FIXED = "not_fixed", "Not fixed"


class HelpResolution(models.TextChoices):
    """How a help request was closed; blank while it is open."""

    OPEN = "", "Open"
    CONTINUED = "continued", "Continued"
    AUTO_FIXED = "auto_fixed", "Fixed by the setup agent"
    MARKED_FAILED = "marked_failed", "Marked failed"
    SKIPPED = "skipped", "Skipped"
    CANCELLED = "cancelled", "Cancelled"


class CaseHelpRequest(models.Model):
    """One Need help event of a run case: the failing step and the agent's ask."""

    test_run_test_case = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="help_requests"
    )
    # 1-based plan step the case stopped at.
    step_num = models.PositiveSmallIntegerField()
    # Model-written; shown escaped.
    reason = models.TextField(
        max_length=HELP_REQUEST_TEXT_MAX_LENGTH, blank=True, default=""
    )
    ask = models.TextField(
        max_length=HELP_REQUEST_TEXT_MAX_LENGTH, blank=True, default=""
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    auto_fix_state = models.CharField(
        max_length=16, choices=HelpAutoFixState.choices, blank=True, default=""
    )
    resolution = models.CharField(
        max_length=16, choices=HelpResolution.choices, blank=True, default=""
    )
    # Null for ``auto_fixed`` (the setup agent resolved it) and while open.
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    # The person's note, or the setup agent's summary for ``auto_fixed``.
    note = models.TextField(max_length=HELP_NOTE_MAX_LENGTH, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["test_run_test_case"],
                condition=models.Q(resolution=""),
                name="case_help_one_open_request",
            )
        ]
        indexes = [
            models.Index(
                fields=["test_run_test_case", "step_num", "-requested_at"],
                name="case_help_pivot_step_idx",
            )
        ]

    @property
    def is_open(self) -> bool:
        return self.resolution == HelpResolution.OPEN

    def __str__(self) -> str:
        return f"Help request #{self.pk} of TRTC #{self.test_run_test_case_id}"


def _screenshot_upload_path(instance: "TestRunScreenshot", filename: str) -> str:
    return f"screenshots/trtc_{instance.test_run_test_case_id}/{filename}"


class TestRunScreenshot(models.Model):
    test_run_test_case = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="screenshots"
    )
    image = models.ImageField(upload_to=_screenshot_upload_path)
    tool_name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"Screenshot #{self.pk} — {self.tool_name}"
