from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import models


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

    def __str__(self) -> str:
        return self.name


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
