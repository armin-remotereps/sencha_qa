"""Tell the project's members that a test run finished (ntfy)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse

from auto_tester.durations import format_duration
from notifications.services.enqueue_notification import enqueue_notification
from notifications.services.notification_message import (
    NotificationKind,
    NotificationMessage,
)
from notifications.services.ntfy_config import ntfy_configured
from notifications.services.recipients import recipient_user_ids
from projects.models import (
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_durations import run_duration

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RunCaseCounts:
    passed: int
    failed: int
    blocked: int
    cancelled: int


def _case_counts(test_run_id: int) -> _RunCaseCounts:
    row = TestRunTestCase.objects.filter(test_run_id=test_run_id).aggregate(
        passed=Count("id", filter=Q(status=TestRunTestCaseStatus.SUCCESS)),
        failed=Count("id", filter=Q(status=TestRunTestCaseStatus.FAILED)),
        blocked=Count("id", filter=Q(status=TestRunTestCaseStatus.BLOCKED)),
        cancelled=Count("id", filter=Q(status=TestRunTestCaseStatus.CANCELLED)),
    )
    return _RunCaseCounts(
        passed=int(row["passed"] or 0),
        failed=int(row["failed"] or 0),
        blocked=int(row["blocked"] or 0),
        cancelled=int(row["cancelled"] or 0),
    )


def _counts_label(counts: _RunCaseCounts) -> str:
    parts = [
        f"{value} {label}"
        for value, label in (
            (counts.passed, "passed"),
            (counts.failed, "failed"),
            (counts.blocked, "blocked"),
            (counts.cancelled, "cancelled"),
        )
        if value > 0
    ]
    return ", ".join(parts) if parts else "no results"


def _build_message(test_run: TestRun, counts: _RunCaseCounts) -> NotificationMessage:
    project = test_run.project
    prefix = "Run finished with failures" if counts.failed > 0 else "Run finished"
    body = f"{test_run.display_name}: {_counts_label(counts)}"
    duration = run_duration(test_run)
    if duration is not None:
        body = f"{body} · {format_duration(duration)}"
    return NotificationMessage.create(
        kind=NotificationKind.RUN_FINISHED,
        title=f"{prefix} · {project.name}",
        body=body,
        path=reverse(
            "projects:test_run_detail",
            kwargs={"project_id": project.pk, "test_run_id": test_run.pk},
        ),
    )


def notify_run_finished(run_id: int, *, generation: int) -> None:
    """Notify every project member who opted into Run finished notifications.

    Call it on commit of the single conditional STARTED -> DONE transition.
    Sends nothing unless the run is still Done on ``generation`` (a newer
    Start, Resume or Re-run owns it otherwise). Enqueues
    ``notifications.tasks.send_notification`` on commit and never raises.
    """
    try:
        if not ntfy_configured():
            return
        test_run = (
            TestRun.objects.select_related("project")
            .only(
                "name",
                "started_at",
                "finished_at",
                "project",
                "project__name",
            )
            .filter(
                pk=run_id,
                status=TestRunStatus.DONE,
                execution_generation=generation,
            )
            .first()
        )
        if test_run is None:
            return
        user_ids = recipient_user_ids(
            test_run.project_id, NotificationKind.RUN_FINISHED
        )
        if not user_ids:
            return
        message = _build_message(test_run, _case_counts(test_run.pk))
        transaction.on_commit(lambda: enqueue_notification(user_ids, message))
    except Exception:
        logger.exception("Run finished notification failed for run %s", run_id)
