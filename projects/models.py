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


class TestRunStatus(models.TextChoices):
    WAITING = "waiting", "Waiting"
    STARTED = "started", "Started"
    DONE = "done", "Done"
    CANCELLED = "cancelled", "Cancelled"


class TestRunTestCaseStatus(models.TextChoices):
    CREATED = "created", "Created"
    IN_PROGRESS = "in_progress", "In Progress"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True, db_index=True)

    def __str__(self) -> str:
        return self.name


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
    agent_system_info = models.JSONField(default=dict, blank=True)
    last_connected_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class TestCaseUpload(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="uploads"
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="uploads"
    )
    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to="uploads/testrail_xml/")
    status = models.CharField(
        max_length=20,
        choices=UploadStatus.choices,
        default=UploadStatus.PENDING,
        db_index=True,
    )
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    total_cases = models.PositiveIntegerField(default=0)
    processed_cases = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.original_filename


@dataclass
class ParsedTestCase:
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
    is_converted: bool = False


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"TestRun #{self.pk} — {self.project.name}"


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
    logs = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("test_run", "test_case")

    def __str__(self) -> str:
        return f"TRTC #{self.pk} — {self.test_case.title[:50]}"


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
