from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models

from controller_client.protocol import (
    MAX_VERSION_LENGTH,
    UPDATE_STATUS_DETAIL_MAX_LENGTH,
)

if TYPE_CHECKING:
    from accounts.models import CustomUser


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
    # Asleep: a case needs help, or (Human in the loop) waits for a person to
    # perform a step; nothing in the run executes until it is resolved. Pages
    # tell the two apart by ``projects.case_help_state.RunHelpState``.
    PAUSED = "paused", "Needs help"


# A run in either status holds the project's machine: no other run may start.
ACTIVE_RUN_STATUSES: tuple[str, ...] = (TestRunStatus.STARTED, TestRunStatus.PAUSED)


class RunHelpMode(models.TextChoices):
    """What happens when a case of the run needs help.

    ``HUMAN_IN_THE_LOOP``: a person performs every step and the agent confirms
    it; help requests then behave as in Attended (no auto-fix). The value is
    short because ``TestRun.help_mode`` is ``max_length=16``.
    """

    ATTENDED = "attended", "Attended"
    NIGHT = "night", "Night (auto-fix first)"
    HUMAN_IN_THE_LOOP = "hitl", "Human in the loop"


class ProjectRunModeScope(StrEnum):
    """Which existing runs follow a new project run mode (form ``apply_to``)."""

    # New runs only.
    NEW_ONLY = "new_only"
    # New runs and drafts (WAITING).
    DRAFTS = "drafts"
    # Also running and paused runs.
    OPEN = "open"
    # All runs, including finished (DONE, CANCELLED) ones.
    ALL = "all"


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
    # Human in the loop: a step was handed to a person (``CasePersonStep``);
    # the checkpoint and the machine state are kept. Not a finished status.
    AWAITING_PERSON = "awaiting_person", "Waiting for a person"
    # A person flagged the result as untrustworthy (``retest_*`` fields say who
    # and from which status). Settled: the run finishes with it; Re-run or
    # Reset clears it, Undo restores ``retest_from_status``.
    NEEDS_RETEST = "needs_retest", "Needs re-test"


# Statuses after which a case's ``active_duration`` is final.
FINISHED_CASE_STATUSES = frozenset(
    {
        TestRunTestCaseStatus.SUCCESS,
        TestRunTestCaseStatus.FAILED,
        TestRunTestCaseStatus.BLOCKED,
        TestRunTestCaseStatus.CANCELLED,
        TestRunTestCaseStatus.NEEDS_RETEST,
    }
)

# Verdicts that Undo of a Needs re-test mark can restore.
RETEST_VERDICT_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
)
RETEST_NOTE_MAX_LENGTH = 2000
# A case's Needs re-test mark fields (``TestRunTestCase.retest_*``) and the
# `QuerySet.update` values that clear them (Re-run, Reset, Undo).
RETEST_FIELDS: tuple[str, ...] = (
    "retest_from_status",
    "retest_marked_at",
    "retest_marked_by",
    "retest_note",
)
RETEST_CLEARED: dict[str, object] = {
    "retest_from_status": "",
    "retest_marked_at": None,
    "retest_marked_by": None,
    "retest_note": "",
}


class BlockedReason(models.TextChoices):
    """Why a case could not be executed or judged; never a verdict on the app.

    Also classifies the fault behind a help request (``CaseHelpRequest.fault``).
    Only a person marks a case Blocked: ``HELP_SKIPPED`` (Mark blocked) or
    ``PERSON_REVIEW`` (an applied result change); the other values remain on
    older Blocked rows.
    """

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
    HELP_SKIPPED = "help_skipped", "Marked blocked by a person"
    PERSON_REVIEW = "person_review", "Marked blocked by a person after review"


class FailedBy(models.TextChoices):
    """Who made a run case Failed; meaningful only while its status is FAILED.

    Readers always filter on ``status=FAILED`` too, so a value left behind by a
    re-run or a result change is never shown. Blank: not recorded (rows older
    than the field).
    """

    AGENT = "agent", "AI verdict"
    HELP = "help", "Marked failed (help)"
    REVIEW = "review", "Changed after review"


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


class ProjectRole(models.TextChoices):
    MEMBER = "member", "Member"
    READ_ONLY = "read_only", "Read-only"


# Roles that may change a project. Every "can write" check and recipient
# query uses this, so a new role only needs adding here.
WRITABLE_ROLES: frozenset[str] = frozenset({ProjectRole.MEMBER})


class Project(models.Model):
    name = models.CharField(max_length=255)
    tags = models.ManyToManyField(Tag, blank=True, related_name="projects")
    members: models.ManyToManyField[CustomUser, ProjectMembership] = (
        models.ManyToManyField(
            settings.AUTH_USER_MODEL,
            through="ProjectMembership",
            blank=True,
            related_name="projects",
        )
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
    # The run mode of every new run in the project; set by a superuser on the
    # Settings tab (``project_run_mode_services``).
    default_help_mode = models.CharField(
        max_length=16,
        choices=RunHelpMode.choices,
        default=RunHelpMode.HUMAN_IN_THE_LOOP,
    )

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


class ProjectMembership(models.Model):
    """One member of a project and their role.

    Keeps the table and column of the former auto-created ``Project.members``
    M2M (migration 0041 is state-only plus the ``role`` column).
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_memberships",
        db_column="customuser_id",
    )
    role = models.CharField(
        max_length=16, choices=ProjectRole.choices, default=ProjectRole.MEMBER
    )

    class Meta:
        db_table = "projects_project_members"
        unique_together = [("project", "user")]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.project_id} ({self.role})"

    @property
    def can_write(self) -> bool:
        return self.role in WRITABLE_ROLES


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
    # That run's TestRail test for this case (tests/view/<id>); set with it.
    testrail_source_test_id = models.PositiveIntegerField(null=True, blank=True)
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
    # Set from ``Project.default_help_mode`` when the run is created; chosen
    # at Start; kept by Resume and Re-run in place unless changed there.
    help_mode = models.CharField(
        max_length=16,
        choices=RunHelpMode.choices,
        default=RunHelpMode.HUMAN_IN_THE_LOOP,
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
    # Claim marker of the morning Night digest: set once, when a digest covers it.
    night_digest_sent_at = models.DateTimeField(null=True, blank=True)

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
    # Meaningful only while status is BLOCKED (kept under a Needs re-test mark
    # so Undo restores it).
    blocked_reason = models.CharField(
        max_length=32, choices=BlockedReason.choices, blank=True, default=""
    )
    # Meaningful only while status is FAILED (see ``FailedBy``).
    failed_by = models.CharField(
        max_length=16, choices=FailedBy.choices, blank=True, default=""
    )
    # Meaningful only while status is NEEDS_RETEST: the status it was marked
    # from, who marked it, when and why.
    retest_from_status = models.CharField(
        max_length=20, choices=TestRunTestCaseStatus.choices, blank=True, default=""
    )
    retest_marked_at = models.DateTimeField(null=True, blank=True)
    retest_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    retest_note = models.TextField(
        max_length=RETEST_NOTE_MAX_LENGTH, blank=True, default=""
    )
    logs = models.TextField(blank=True, default="")
    # First pick-up by a worker since the case last started from scratch.
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # When the worker that owns the case now picked it up; null when none does.
    segment_started_at = models.DateTimeField(null=True, blank=True)
    # Sum of the closed execution segments since the last from-scratch start
    # (``projects.case_duration_services``); null means unknown.
    active_duration = models.DurationField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    testrail_result_id = models.PositiveIntegerField(null=True, blank=True)
    # TestRail test (run + case) the pushed result went to; its page shows results.
    testrail_test_id = models.PositiveIntegerField(null=True, blank=True)
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
    # Who the case's result belongs to on the leaderboard: the person who last
    # drove it (Start, Resume, Re-run, help resolution, person step), set by
    # ``projects.case_credit.credit_cases``. Null: nobody known.
    credited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        db_index=False,
    )
    # Set once the result is added to the source run; the dedupe key.
    testrail_source_result_id = models.PositiveIntegerField(null=True, blank=True)
    # TestRail test in the source run: snapshot of test_case.testrail_source_test_id
    # at creation, else set by the first send; kept across re-sends.
    testrail_source_test_id = models.PositiveIntegerField(null=True, blank=True)
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
        indexes = [
            # Failed cases list, newest first (built concurrently by 0035).
            models.Index(
                fields=["status", "-finished_at"], name="run_case_status_finished_idx"
            )
        ]

    @property
    def shown_duration(self) -> timedelta | None:
        """The case's time taken once it has finished; None (shown "—") while
        it is queued, running or waiting for help, whose total is partial."""
        if self.status not in FINISHED_CASE_STATUSES:
            return None
        return self.active_duration

    def __str__(self) -> str:
        return f"TRTC #{self.pk} — {self.test_case.title[:50]}"


class CaseRestartKind(models.TextChoices):
    RESUME = "resume", "Resumed"
    RERUN = "rerun", "Re-run"
    REDESIGN = "redesign", "Re-run with a redesigned plan"


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
        max_length=16, choices=CaseRestartKind.choices, default=CaseRestartKind.RESUME
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
    SKIPPED = "skipped", "Marked blocked"
    MARKED_RETEST = "marked_retest", "Marked for re-test"
    CANCELLED = "cancelled", "Cancelled"


class HelpProposalAction(models.TextChoices):
    """What the help-chat agent suggests; blank when it suggests nothing."""

    NONE = "", "None"
    CONTINUE = "continue", "Continue"
    MARK_FAILED = "mark_failed", "Mark failed"
    SKIP = "skip", "Mark blocked"


class HelpContinueMode(models.TextChoices):
    """How a case goes on after Continue (or a fixed auto-fix)."""

    # Re-run the step the case stopped at from the screen as it is now.
    CURRENT_SCREEN = "current_screen", "Re-run the step from the current screen"
    # Clean up, restore the earlier steps' state, then run that step.
    RESTORE = "restore", "Restore the earlier steps, then run the step"
    # No usable checkpoint: plan and run the case from the beginning.
    FROM_SCRATCH = "from_scratch", "Start the case again"


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
    # The infrastructure fault that paused the case; empty when the agent asked.
    fault = models.CharField(
        max_length=32, choices=BlockedReason.choices, blank=True, default=""
    )
    # Decided when the case paused (``projects.case_faults.help_continue_mode``).
    continue_mode = models.CharField(
        max_length=16,
        choices=HelpContinueMode.choices,
        default=HelpContinueMode.CURRENT_SCREEN,
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    auto_fix_state = models.CharField(
        max_length=16, choices=HelpAutoFixState.choices, blank=True, default=""
    )
    # When ``auto_fix_state`` last became ``running``; null before (and on rows
    # older than the field). The stale sweep measures a lost start task from it.
    auto_fix_started_at = models.DateTimeField(null=True, blank=True)
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
    # The person's note; empty for ``auto_fixed`` and while open.
    note = models.TextField(max_length=HELP_NOTE_MAX_LENGTH, blank=True, default="")
    # The setup agent's report of its auto-fix (fixed or not fixed); empty when
    # no auto-fix ran or it has not ended yet. Shown escaped.
    auto_fix_summary = models.TextField(
        max_length=HELP_NOTE_MAX_LENGTH, blank=True, default=""
    )
    # What the help-chat agent suggests the person do; only the person acts.
    agent_proposal_action = models.CharField(
        max_length=16, choices=HelpProposalAction.choices, blank=True, default=""
    )
    agent_proposal_note = models.TextField(
        max_length=HELP_NOTE_MAX_LENGTH, blank=True, default=""
    )
    agent_proposal_at = models.DateTimeField(null=True, blank=True)

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
            ),
            # Leaderboard "Help resolved" window (built concurrently by 0048).
            models.Index(fields=["resolved_at"], name="case_help_resolved_at_idx"),
        ]

    @property
    def is_open(self) -> bool:
        return self.resolution == HelpResolution.OPEN

    def __str__(self) -> str:
        return f"Help request #{self.pk} of TRTC #{self.test_run_test_case_id}"


PLAN_REVISION_REASON_MAX_LENGTH = 1000


class PlanRevisionStatus(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    APPLIED = "applied", "Applied"
    # By a person.
    DISCARDED = "discarded", "Discarded"
    # By a newer draft, or the request was resolved without applying it.
    SUPERSEDED = "superseded", "Superseded"


class PlanRevisionSource(models.TextChoices):
    # The help chat of a paused case (has a help request).
    HELP = "help", "Help chat"
    # The result chat of a finished case (no help request).
    REVIEW = "review", "Result review"


class CasePlanRevision(models.Model):
    """An agent's redesign of a case's whole test plan (a draft until applied).

    ``source`` ``help``: proposed in a help request's chat; ``review``: proposed
    in the case's result chat, with no help request. Help code filters by
    ``help_request`` and so never sees review rows.

    ``steps`` / ``previous_steps`` use the JSON forms of ``agents.plan_revision``
    (``revision_steps_to_json`` / ``snapshots_to_json``).
    """

    test_run_test_case = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="plan_revisions"
    )
    help_request = models.ForeignKey(
        CaseHelpRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="plan_revisions",
    )
    source = models.CharField(
        max_length=8,
        choices=PlanRevisionSource.choices,
        default=PlanRevisionSource.HELP,
    )
    # ``agents.plan_revision.plan_fingerprint`` of the checkpoint it was built on.
    base_fingerprint = models.CharField(max_length=64)
    steps = models.JSONField(default=list)
    previous_steps = models.JSONField(default=list)
    # First new step that is not carried.
    resume_step = models.PositiveSmallIntegerField()
    # Model-written; shown escaped.
    reason = models.TextField(
        max_length=PLAN_REVISION_REASON_MAX_LENGTH, blank=True, default=""
    )
    status = models.CharField(
        max_length=16,
        choices=PlanRevisionStatus.choices,
        default=PlanRevisionStatus.PROPOSED,
    )
    proposed_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    # ``machine_setup.SetupConversation`` id, kept as a plain id so ``projects``
    # has no schema dependency on ``machine_setup``.
    conversation_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["help_request"],
                condition=models.Q(status="proposed"),
                name="plan_revision_one_pending",
            ),
            models.UniqueConstraint(
                fields=["test_run_test_case"],
                condition=models.Q(status="proposed", source="review"),
                name="plan_revision_one_pending_review",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source="help", help_request__isnull=False)
                    | models.Q(source="review", help_request__isnull=True)
                ),
                name="plan_revision_source_matches_request",
            ),
        ]
        indexes = [
            models.Index(
                fields=["help_request", "-proposed_at"],
                name="plan_revision_request_idx",
            )
        ]

    def __str__(self) -> str:
        if self.help_request_id is None:
            return f"Plan revision #{self.pk} of TRTC #{self.test_run_test_case_id}"
        return f"Plan revision #{self.pk} of help request #{self.help_request_id}"


# Caps on the model-written texts of a result change.
RESULT_CHANGE_SUMMARY_MAX_LENGTH = 4000
RESULT_CHANGE_EXPLANATION_MAX_LENGTH = 2000


class ResultChangeStatus(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    APPLIED = "applied", "Applied"
    # By a person.
    DISCARDED = "discarded", "Discarded"
    # By a newer proposal, or the case was re-run with a redesigned plan.
    SUPERSEDED = "superseded", "Superseded"


class CaseResultChange(models.Model):
    """A result-chat agent's proposed change of a finished case's result.

    A person applies it; APPLIED rows are the audit trail. ``from_*`` snapshot
    the result when proposed and are rewritten at apply with the values
    actually replaced.
    """

    test_run_test_case = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="result_changes"
    )
    # sha256 of (status, blocked_reason, finished_at ISO, result) it was built on.
    base_fingerprint = models.CharField(max_length=64)
    from_status = models.CharField(max_length=20, choices=TestRunTestCaseStatus.choices)
    from_blocked_reason = models.CharField(
        max_length=32, choices=BlockedReason.choices, blank=True, default=""
    )
    from_result = models.TextField(blank=True, default="")
    to_status = models.CharField(max_length=20, choices=TestRunTestCaseStatus.choices)
    # Model-written; shown escaped. ``summary`` becomes the new result text.
    summary = models.TextField(max_length=RESULT_CHANGE_SUMMARY_MAX_LENGTH)
    explanation = models.TextField(
        max_length=RESULT_CHANGE_EXPLANATION_MAX_LENGTH, blank=True, default=""
    )
    status = models.CharField(
        max_length=16,
        choices=ResultChangeStatus.choices,
        default=ResultChangeStatus.PROPOSED,
    )
    proposed_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    # ``machine_setup.SetupConversation`` id, kept as a plain id so ``projects``
    # has no schema dependency on ``machine_setup``.
    conversation_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["test_run_test_case"],
                condition=models.Q(status="proposed"),
                name="result_change_one_pending",
            )
        ]
        indexes = [
            models.Index(
                fields=["test_run_test_case", "-proposed_at"],
                name="result_change_case_idx",
            )
        ]

    def __str__(self) -> str:
        return f"Result change #{self.pk} of TRTC #{self.test_run_test_case_id}"


# Caps on the texts of a person step (Human in the loop). The database does
# not enforce them; services truncate.
PERSON_STEP_INSTRUCTION_MAX_LENGTH = 2000
PERSON_STEP_EXPECTED_MAX_LENGTH = 1000
PERSON_STEP_COMMAND_MAX_LENGTH = 2000
PERSON_STEP_OUTPUT_MAX_LENGTH = 20000
PERSON_STEP_AGENT_TEXT_MAX_LENGTH = 1000
# "How to do it" items of a hand-off and the Ask AI questions of a step.
PERSON_STEP_HOW_TO_MAX_ITEMS = 8
PERSON_STEP_HOW_TO_TEXT_MAX_LENGTH = 400
PERSON_STEP_HOW_TO_COMMAND_MAX_LENGTH = 500
# Test case steps a person step covers. A row keeps every covered step (the
# plan's own cap, ``agents.case_step_refs.MAX_CASE_STEP_REFS``); the Test
# Runner gets the first ``controller_client.protocol``
# PERSON_STEP_CASE_STEPS_MAX_ITEMS. Texts are cut to the same length there.
PERSON_STEP_CASE_STEPS_MAX_ITEMS = 50
PERSON_STEP_CASE_STEP_TEXT_MAX_LENGTH = 1000
PERSON_STEP_CASE_STEPS_LABEL_MAX_LENGTH = 255
PERSON_STEP_QUESTION_MAX_LENGTH = 1000
PERSON_STEP_ANSWER_MAX_LENGTH = 4000
PERSON_STEP_QUESTIONS_PER_STEP = 20


class PersonStepKind(models.TextChoices):
    """What a person is asked to do in a Human in the loop run."""

    INTERACTIVE = "interactive", "Interactive step"
    # A command the test case states; the person runs it in the machine's shell.
    COMMAND = "command", "Command"
    # The state restore before a resumed case (replaces the AI restore).
    RESTORE = "restore", "Restore the earlier steps"


class PersonStepStatus(models.TextChoices):
    # The person's turn.
    WAITING = "waiting", "Waiting for a person"
    # The agent is confirming the report.
    REPORTED = "reported", "Being confirmed"
    # Open again: the agent could not confirm the report (``agent_objection``).
    NOT_CONFIRMED = "not_confirmed", "Not confirmed"
    PASSED = "passed", "Passed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    # The run left Human in the loop; the agent runs the step itself.
    HANDED_BACK = "handed_back", "Handed back to the agent"


# Statuses of a person step that is still open (one per case at most).
OPEN_PERSON_STEP_STATUSES: tuple[str, ...] = (
    PersonStepStatus.WAITING,
    PersonStepStatus.REPORTED,
    PersonStepStatus.NOT_CONFIRMED,
)


class PersonStepOutcome(models.TextChoices):
    """The person's report; blank until they report."""

    AS_EXPECTED = "as_expected", "As expected"
    DIFFERENT = "different", "The result was different"


class PersonStepClose(models.TextChoices):
    """How a person step ended; blank while it is open."""

    OPEN = "", "Open"
    CONFIRMED = "confirmed", "Confirmed by the agent"
    OVERRULED = "overruled", "Overruled by a person"
    REPORTED_DIFFERENT = "reported_different", "Reported a different result"
    MARKED_FAILED = "marked_failed", "Marked failed"
    MARKED_BLOCKED = "marked_blocked", "Marked blocked"
    MARKED_RETEST = "marked_retest", "Marked for re-test"


class PersonStepChannel(models.TextChoices):
    """Where a person answered a person step; blank until a person acts.

    Rows from before the channel existed keep blank and were answered on the
    web (``reported_by``/``closed_by`` name the user). A Test Runner answer has
    a user only when the runner's ``CONTROLLER_PERSON_EMAIL`` named an active
    project member.
    """

    NONE = "", "Not answered by a person"
    WEB = "web", "Punk Hazard"
    RUNNER = "runner", "Test Runner"


class CasePersonStep(models.Model):
    """One step of a run case handed to a person (Human in the loop).

    A redo after "not confirmed" reuses the row and bumps ``round``.
    """

    test_run_test_case = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="person_steps"
    )
    # 1-based plan step; for ``restore`` the step the restore prepares.
    step_num = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=16, choices=PersonStepKind.choices)
    # 1-based hand-off round of this step.
    round = models.PositiveSmallIntegerField(default=1)
    # Model-written; shown escaped.
    instruction = models.TextField(max_length=PERSON_STEP_INSTRUCTION_MAX_LENGTH)
    expected_result = models.TextField(
        max_length=PERSON_STEP_EXPECTED_MAX_LENGTH, blank=True, default=""
    )
    # Model-written, taken from the test case text; blank unless ``command``.
    command = models.TextField(
        max_length=PERSON_STEP_COMMAND_MAX_LENGTH, blank=True, default=""
    )
    # Server-derived from the runner's OS (e.g. "Terminal (bash)"); never
    # model-chosen.
    shell_label = models.CharField(max_length=64, blank=True, default="")
    # Model-written "How to do it" items: a list of ``{"text", "command"}``
    # (commands are AI suggestions, not case-stated), capped by the
    # ``PERSON_STEP_HOW_TO_*`` constants; shown escaped.
    how_to = models.JSONField(default=list, blank=True)
    # The numbered test case steps the plan step covers, as
    # ``[{"num", "text", "expected"}]`` copied from the case when the step was
    # handed out (``projects.case_step_refs.case_step_rows``, capped), and
    # their label ("TestRail steps 3–5"); empty for restores, free-text cases
    # and older rows. Shown escaped.
    case_steps = models.JSONField(default=list, blank=True)
    case_steps_label = models.CharField(
        max_length=PERSON_STEP_CASE_STEPS_LABEL_MAX_LENGTH, blank=True, default=""
    )
    status = models.CharField(
        max_length=16,
        choices=PersonStepStatus.choices,
        default=PersonStepStatus.WAITING,
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    reported_at = models.DateTimeField(null=True, blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Set with every report.
    reported_via = models.CharField(
        max_length=8, choices=PersonStepChannel.choices, blank=True, default=""
    )
    outcome = models.CharField(
        max_length=16, choices=PersonStepOutcome.choices, blank=True, default=""
    )
    # The person's note and pasted command output; shown escaped.
    note = models.TextField(max_length=HELP_NOTE_MAX_LENGTH, blank=True, default="")
    output = models.TextField(
        max_length=PERSON_STEP_OUTPUT_MAX_LENGTH, blank=True, default=""
    )
    # The agent's confirmation text; and its reason while ``not_confirmed``.
    agent_summary = models.TextField(
        max_length=PERSON_STEP_AGENT_TEXT_MAX_LENGTH, blank=True, default=""
    )
    agent_objection = models.TextField(
        max_length=PERSON_STEP_AGENT_TEXT_MAX_LENGTH, blank=True, default=""
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Set by a person's overrule, Mark failed or Mark blocked; a runner
    # overrule may have no ``closed_by``, so this marks it as a person's.
    closed_via = models.CharField(
        max_length=8, choices=PersonStepChannel.choices, blank=True, default=""
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(
        max_length=24, choices=PersonStepClose.choices, blank=True, default=""
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["test_run_test_case"],
                condition=models.Q(status__in=OPEN_PERSON_STEP_STATUSES),
                name="person_step_one_open",
            )
        ]
        indexes = [
            models.Index(
                fields=["test_run_test_case", "step_num", "-requested_at"],
                name="person_step_pivot_step_idx",
            ),
            # Leaderboard "Steps performed" window (built concurrently by 0048).
            models.Index(fields=["reported_at"], name="person_step_reported_at_idx"),
        ]

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_PERSON_STEP_STATUSES

    def __str__(self) -> str:
        return f"Person step #{self.pk} of TRTC #{self.test_run_test_case_id}"


class PersonStepQuestionStatus(models.TextChoices):
    PENDING = "pending", "Waiting for the AI"
    ANSWERED = "answered", "Answered"
    FAILED = "failed", "Not answered"


class PersonStepQuestion(models.Model):
    """A person's question to the AI about the step waiting for them.

    Advisory only: answering never changes the step, case or run. At most one
    ``pending`` question per step; a stale pending one is failed lazily.
    """

    person_step = models.ForeignKey(
        CasePersonStep, on_delete=models.CASCADE, related_name="questions"
    )
    # The step round it was asked in.
    round = models.PositiveSmallIntegerField()
    # Person-written and model-written; shown escaped.
    question = models.TextField(max_length=PERSON_STEP_QUESTION_MAX_LENGTH)
    answer = models.TextField(
        max_length=PERSON_STEP_ANSWER_MAX_LENGTH, blank=True, default=""
    )
    status = models.CharField(
        max_length=16,
        choices=PersonStepQuestionStatus.choices,
        default=PersonStepQuestionStatus.PENDING,
    )
    asked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    asked_via = models.CharField(max_length=8, choices=PersonStepChannel.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["person_step"],
                condition=models.Q(status=PersonStepQuestionStatus.PENDING),
                name="person_step_question_one_pending",
            )
        ]
        indexes = [
            models.Index(
                fields=["person_step", "created_at"],
                name="person_step_question_step_idx",
            )
        ]

    def __str__(self) -> str:
        return f"Question #{self.pk} on person step #{self.person_step_id}"


class RunnerUpdateStatus(models.TextChoices):
    """Server-side state of a Test Runner self-update.

    Runner phases map onto these; ``RunnerUpdate.phase`` keeps the exact phase.
    """

    SENT = "sent", "Sent to the Test Runner"
    PREPARING = "preparing", "Preparing"
    # creating_environment, installing_dependencies and installing_browsers.
    INSTALLING = "installing", "Installing"
    STARTING = "starting", "Starting the new Test Runner"
    WAITING = "waiting", "Waiting for the new Test Runner"
    # Terminal.
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    # No outcome within RUNNER_UPDATE_TIMEOUT_SECONDS; written lazily.
    EXPIRED = "expired", "Did not finish"


# Statuses of an update still in flight (one per project at most).
NON_TERMINAL_RUNNER_UPDATE_STATUSES: tuple[str, ...] = (
    RunnerUpdateStatus.SENT,
    RunnerUpdateStatus.PREPARING,
    RunnerUpdateStatus.INSTALLING,
    RunnerUpdateStatus.STARTING,
    RunnerUpdateStatus.WAITING,
)


class RunnerUpdateOldRemoval(models.TextChoices):
    """Whether the new runner deleted the old install folder; blank until it says."""

    PENDING = "", "Not reported"
    REMOVED = "removed", "Removed"
    FAILED = "failed", "Could not be removed"


RUNNER_UPDATE_DETAIL_MAX_LENGTH = UPDATE_STATUS_DETAIL_MAX_LENGTH


class RunnerUpdate(models.Model):
    """One press of Update Test Runner: the audit row and its live state.

    The id is sent to the runner as ``update_id``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="runner_updates"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    from_version = models.CharField(max_length=MAX_VERSION_LENGTH)
    to_version = models.CharField(max_length=MAX_VERSION_LENGTH)
    status = models.CharField(
        max_length=16,
        choices=RunnerUpdateStatus.choices,
        default=RunnerUpdateStatus.SENT,
    )
    # Last ``controller_client.protocol.UpdatePhase`` the runner reported.
    phase = models.CharField(max_length=32, blank=True, default="")
    # Runner-written plain text; shown escaped.
    detail = models.CharField(
        max_length=RUNNER_UPDATE_DETAIL_MAX_LENGTH, blank=True, default=""
    )
    old_removal = models.CharField(
        max_length=16,
        choices=RunnerUpdateOldRemoval.choices,
        blank=True,
        default=RunnerUpdateOldRemoval.PENDING,
    )
    archive_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(status__in=NON_TERMINAL_RUNNER_UPDATE_STATUSES),
                name="runner_update_one_in_flight",
            )
        ]
        indexes = [
            models.Index(
                fields=["project", "-created_at"], name="runner_update_project_idx"
            )
        ]

    @property
    def is_in_flight(self) -> bool:
        return self.status in NON_TERMINAL_RUNNER_UPDATE_STATUSES

    def __str__(self) -> str:
        return (
            f"Runner update {self.from_version} -> {self.to_version} "
            f"of project #{self.project_id}"
        )


def _screenshot_upload_path(instance: "TestRunScreenshot", filename: str) -> str:
    return f"screenshots/trtc_{instance.test_run_test_case_id}/{filename}"


class TestRunScreenshot(models.Model):
    test_run_test_case = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="screenshots"
    )
    image = models.ImageField(upload_to=_screenshot_upload_path)
    tool_name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    # The step attempt that took it (``SubTaskAttempt.attempt_id``); empty for
    # screenshots taken before tagging or outside a sub-agent.
    attempt_id = models.CharField(max_length=32, blank=True, default="")
    step_num = models.PositiveSmallIntegerField(null=True, blank=True)
    # Selected for the next "Send checked to TestRail"; frozen when the case is
    # checked (projects/testrail_evidence_services.py).
    testrail_evidence = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"Screenshot #{self.pk} — {self.tool_name}"
