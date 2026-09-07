from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from accounts.models import CustomUser
from projects.models import (
    Project,
    TestCase,
    TestRun,
    TestRunTestCase,
    TestRunTestCaseStatus,
)


def make_user(
    email: str = "user@example.com", password: str = "password123"
) -> CustomUser:
    """Create a CustomUser for tests."""
    return CustomUser.objects.create_user(email=email, password=password)


def make_project(*, user: CustomUser, name: str = "Test Project") -> Project:
    """Create a Project with `user` as its sole member."""
    project = Project.objects.create(name=name)
    project.members.add(user)
    return project


def make_case(*, project: Project, title: str = "Sample case") -> TestCase:
    """Create a TestCase belonging to `project`, using its model defaults."""
    return TestCase.objects.create(project=project, title=title)


def make_run(*, project: Project, name: str = "") -> TestRun:
    """Create a draft (WAITING) TestRun for `project`."""
    return TestRun.objects.create(project=project, name=name)


def add_case_to_run(*, test_run: TestRun, test_case: TestCase) -> TestRunTestCase:
    """Create the pivot linking `test_case` to `test_run`."""
    return TestRunTestCase.objects.create(test_run=test_run, test_case=test_case)


def finish_pivot(
    pivot: TestRunTestCase,
    *,
    status: TestRunTestCaseStatus,
    finished_at: datetime | None = None,
) -> TestRunTestCase:
    """Mark a pivot finished with the given status, for summary/readiness tests."""
    pivot.status = status
    pivot.finished_at = finished_at or timezone.now()
    pivot.save(update_fields=["status", "finished_at", "updated_at"])
    return pivot
