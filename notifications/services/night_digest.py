"""Morning digest: one ntfy push per user summarising the night's Night runs."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from auto_tester.celery import app as celery_app
from auto_tester.durations import format_duration
from notifications.models import NotificationPreference
from notifications.services.notification_message import (
    NotificationKind,
    NotificationMessage,
)
from notifications.services.ntfy_config import ntfy_configured
from projects.models import (
    WRITABLE_ROLES,
    FailedBy,
    RunHelpMode,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_durations import run_duration

logger = logging.getLogger(__name__)

DIGEST_TITLE = "Night runs"
# Sent by name: notifications.tasks imports this module, so importing the task
# (or enqueue_notification, which does) here would be an import cycle.
SEND_NOTIFICATION_TASK = "notifications.tasks.send_notification"


@dataclass(frozen=True)
class _DigestRun:
    run_id: int
    project_id: int
    label: str
    # Formatted; "" while the run has no finish time (still waiting for help).
    duration: str
    passed: int
    failed: int
    blocked: int
    need_help: int


def _claim_runs(now: datetime) -> list[int]:
    """Mark the unsent Night runs as digested; return the ids this call claimed.

    The rows are locked with SKIP LOCKED and then updated only while still
    unsent, so two beat ticks or workers never claim the same run.
    """
    since = now - timedelta(hours=settings.NIGHT_DIGEST_LOOKBACK_HOURS)
    with transaction.atomic():
        run_ids = list(
            TestRun.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=TestRunStatus.DONE) | Q(status=TestRunStatus.PAUSED),
                help_mode=RunHelpMode.NIGHT,
                night_digest_sent_at__isnull=True,
                started_at__gte=since,
            )
            .values_list("id", flat=True)
        )
        if not run_ids:
            return []
        TestRun.objects.filter(
            id__in=run_ids, night_digest_sent_at__isnull=True
        ).update(night_digest_sent_at=now)
    return run_ids


def _duration_label(test_run: TestRun) -> str:
    duration = run_duration(test_run)
    return "" if duration is None else format_duration(duration)


def _load_runs(run_ids: list[int]) -> list[_DigestRun]:
    counts = {
        row["test_run_id"]: row
        for row in TestRunTestCase.objects.filter(test_run_id__in=run_ids)
        .order_by()
        .values("test_run_id")
        .annotate(
            passed=Count("id", filter=Q(status=TestRunTestCaseStatus.SUCCESS)),
            failed=Count("id", filter=Q(status=TestRunTestCaseStatus.FAILED)),
            blocked=Count("id", filter=Q(status=TestRunTestCaseStatus.BLOCKED)),
            need_help=Count("id", filter=Q(status=TestRunTestCaseStatus.NEED_HELP)),
        )
    }
    runs = (
        TestRun.objects.select_related("project")
        .only("name", "started_at", "finished_at", "project", "project__name")
        .filter(id__in=run_ids)
        .order_by("started_at", "id")
    )
    result: list[_DigestRun] = []
    for test_run in runs:
        row = counts.get(test_run.pk, {})
        result.append(
            _DigestRun(
                run_id=test_run.pk,
                project_id=test_run.project_id,
                label=f"{test_run.project.name} · {test_run.display_name}",
                duration=_duration_label(test_run),
                passed=int(row.get("passed", 0)),
                failed=int(row.get("failed", 0)),
                blocked=int(row.get("blocked", 0)),
                need_help=int(row.get("need_help", 0)),
            )
        )
    return result


def _recipients_by_project(project_ids: list[int]) -> dict[int, set[int]]:
    """User id -> the claimed projects they are an active writable member of.

    One query; project and role sit in one ``filter()`` so both conditions and
    the selected project id use the same membership row.
    """
    by_user: dict[int, set[int]] = defaultdict(set)
    rows = NotificationPreference.objects.filter(
        enabled=True,
        notify_night_digest=True,
        user__is_active=True,
        user__project_memberships__project_id__in=project_ids,
        user__project_memberships__role__in=WRITABLE_ROLES,
    ).values_list("user_id", "user__project_memberships__project_id")
    for user_id, project_id in rows:
        by_user[user_id].add(project_id)
    return by_user


def _counts_label(passed: int, failed: int, blocked: int, need_help: int) -> str:
    parts = [
        f"{value} {label}"
        for value, label in (
            (passed, "passed"),
            (failed, "failed"),
            (blocked, "blocked"),
            (need_help, "needs help" if need_help == 1 else "need help"),
        )
        if value > 0
    ]
    return ", ".join(parts) if parts else "no results"


def _build_message(
    runs: list[_DigestRun], *, failures_path: str, home_path: str
) -> NotificationMessage:
    passed = sum(run.passed for run in runs)
    failed = sum(run.failed for run in runs)
    blocked = sum(run.blocked for run in runs)
    need_help = sum(run.need_help for run in runs)
    run_word = "run" if len(runs) == 1 else "runs"
    summary = f"{len(runs)} {run_word}, " + _counts_label(
        passed, failed, blocked, need_help
    )
    lines = [
        f"{run.label}: "
        f"{_counts_label(run.passed, run.failed, run.blocked, run.need_help)}"
        + (f" · {run.duration}" if run.duration else "")
        for run in runs
    ]
    # Help only (nothing failed): the dashboard's attention list is the place.
    path = failures_path if failed > 0 or need_help == 0 else home_path
    return NotificationMessage.create(
        kind=NotificationKind.NIGHT_DIGEST,
        title=f"{DIGEST_TITLE}: {summary}",
        body="; ".join(lines),
        path=path,
    )


def _enqueue(user_id: int, message: NotificationMessage) -> None:
    """Queue one digest; logs and never raises. Only the user id goes in the args."""
    try:
        celery_app.send_task(
            SEND_NOTIFICATION_TASK,
            args=[
                [user_id],
                message.kind.value,
                message.title,
                message.body,
                message.path,
            ],
        )
    except Exception:
        logger.exception("Could not enqueue the night digest notification")


def send_night_digests(now: datetime) -> int:
    """Claim the unsent Night runs and push one digest per opted-in member.

    Covers Night runs started within ``NIGHT_DIGEST_LOOKBACK_HOURS`` that are
    Done or waiting for help; each run is digested once. Runs are claimed even
    when ntfy is not configured, so turning it on later sends no backlog.
    Returns the number of runs claimed. Task arguments carry user ids and
    text only, never topics.
    """
    # Resolved before claiming, so a routing error cannot lose claimed runs.
    since_date = timezone.localdate(
        now - timedelta(hours=settings.NIGHT_DIGEST_LOOKBACK_HOURS)
    )
    failures_path = (
        reverse("dashboard:failed_cases")
        + "?"
        + urlencode(
            {"failed_by": FailedBy.AGENT.value, "date_from": since_date.isoformat()}
        )
    )
    home_path = reverse("dashboard:index")

    run_ids = _claim_runs(now)
    if not run_ids or not ntfy_configured():
        return len(run_ids)
    runs = _load_runs(run_ids)
    by_user = _recipients_by_project(sorted({run.project_id for run in runs}))
    for user_id, project_ids in by_user.items():
        user_runs = [run for run in runs if run.project_id in project_ids]
        if not user_runs:
            continue
        message = _build_message(
            user_runs, failures_path=failures_path, home_path=home_path
        )
        _enqueue(user_id, message)
    logger.info(
        "Night digest: %d run(s) claimed, %d user(s) queued", len(run_ids), len(by_user)
    )
    return len(run_ids)
