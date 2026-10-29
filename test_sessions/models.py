from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from projects.models import Project, TestRun, TestRunTestCase
from test_sessions.case_activity import CaseActivity


class TestSession(models.Model):
    """A stretch of testing by one user; open while ``ended_at`` is null."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="test_sessions",
    )
    name = models.CharField(max_length=120, blank=True, default="")
    note = models.TextField(max_length=2000, blank=True, default="")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    # Ended by the stale-session beat task, not by the user.
    closed_automatically = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(ended_at__isnull=True),
                name="test_session_one_open_per_user",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F("started_at")),
                name="test_session_ends_after_start",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "-started_at"], name="test_session_user_started_idx"
            ),
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        started = timezone.localtime(self.started_at)
        return f"Session of {started:%d %b %Y, %H:%M}"


class TestSessionCase(models.Model):
    """One run case a tester acted on in a session (unique per session).

    While ``frozen_at`` is null the outcome is read from the live run case;
    once its verdict is about to be discarded (Reset, Resume, Re-run) or the
    session stops, the outcome is copied into the ``frozen_*`` columns. The
    names are snapshots so rows of deleted runs still export.
    """

    session = models.ForeignKey(
        TestSession, on_delete=models.CASCADE, related_name="cases"
    )
    test_run_test_case = models.ForeignKey(
        TestRunTestCase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    test_run = models.ForeignKey(
        TestRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    project_name = models.CharField(max_length=255, blank=True, default="")
    run_name = models.CharField(max_length=255, blank=True, default="")
    run_ref = models.PositiveIntegerField(null=True, blank=True)
    pivot_ref = models.PositiveIntegerField(null=True, blank=True)
    case_title = models.CharField(max_length=500, blank=True, default="")
    testrail_case_id = models.CharField(max_length=50, blank=True, default="")

    first_activity = models.CharField(max_length=20, choices=CaseActivity.choices)
    first_activity_at = models.DateTimeField()
    last_activity = models.CharField(max_length=20, choices=CaseActivity.choices)
    last_activity_at = models.DateTimeField()
    activity_count = models.PositiveIntegerField(default=1)
    person_step_count = models.PositiveIntegerField(default=0)
    # The run's help mode at the last activity ("hitl": a person performed it).
    run_help_mode = models.CharField(max_length=16, blank=True, default="")

    frozen_at = models.DateTimeField(null=True, blank=True)
    frozen_status = models.CharField(max_length=20, blank=True, default="")
    frozen_failed_by = models.CharField(max_length=16, blank=True, default="")
    frozen_blocked_reason = models.CharField(max_length=32, blank=True, default="")
    frozen_started_at = models.DateTimeField(null=True, blank=True)
    frozen_finished_at = models.DateTimeField(null=True, blank=True)
    frozen_active_duration = models.DurationField(null=True, blank=True)
    frozen_result = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "test_run_test_case"],
                name="test_session_case_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["session", "id"], name="test_session_case_page_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.case_title} (session {self.session_id})"
