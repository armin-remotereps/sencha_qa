# Hand-written: best-effort leaderboard credit for recent and open cases.

from collections.abc import Iterable
from datetime import datetime, timedelta

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q
from django.utils import timezone

# Covers the current and the previous 7-day leaderboard windows at deploy time.
_BACKFILL_DAYS = 14
_BATCH_SIZE = 1000
# Same values as projects.models.TestRunTestCaseStatus / HelpResolution and
# test_sessions.case_activity.CaseActivity.
_OPEN_STATUSES = ("need_help", "awaiting_person")
# projects.case_credit.CREDITED_HELP_RESOLUTIONS (Continue, Mark failed, Mark
# blocked): a re-test mark or a cancel never moves the credit.
_CREDITED_RESOLUTIONS = ("continued", "marked_failed", "skipped")
_CHUNK_SIZE = 2000
_REVIEW_ACTIVITIES = ("retest", "result_change")


def backfill_case_credit(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Credit each recent or open case to the user of its latest known action.

    Signals: resumes / re-runs, help resolutions (Continue, Mark failed, Mark
    blocked), reported person steps and test session activity (not reviews).
    Cases with none stay unattributed; the first Start of a run was never
    stored.
    """
    case_model = apps.get_model("projects", "TestRunTestCase")
    resume_model = apps.get_model("projects", "TestRunCaseResume")
    help_model = apps.get_model("projects", "CaseHelpRequest")
    step_model = apps.get_model("projects", "CasePersonStep")
    session_case_model = apps.get_model("test_sessions", "TestSessionCase")

    cutoff = timezone.now() - timedelta(days=_BACKFILL_DAYS)
    scope = case_model.objects.filter(
        Q(finished_at__gte=cutoff) | Q(status__in=_OPEN_STATUSES)
    ).values("id")

    latest: dict[int, tuple[datetime, int]] = {}

    def offer(rows: Iterable[tuple[int, int | None, datetime | None]]) -> None:
        for case_id, user_id, at in rows:
            if user_id is None or at is None:
                continue
            current = latest.get(case_id)
            if current is None or at > current[0]:
                latest[case_id] = (at, user_id)

    offer(
        resume_model.objects.filter(test_run_test_case_id__in=scope)
        .values_list("test_run_test_case_id", "resumed_by_id", "created_at")
        .iterator(chunk_size=_CHUNK_SIZE)
    )
    offer(
        help_model.objects.filter(test_run_test_case_id__in=scope)
        .filter(resolution__in=_CREDITED_RESOLUTIONS)
        .values_list("test_run_test_case_id", "resolved_by_id", "resolved_at")
        .iterator(chunk_size=_CHUNK_SIZE)
    )
    offer(
        step_model.objects.filter(test_run_test_case_id__in=scope)
        .values_list("test_run_test_case_id", "reported_by_id", "reported_at")
        .iterator(chunk_size=_CHUNK_SIZE)
    )
    offer(
        session_case_model.objects.filter(test_run_test_case_id__in=scope)
        .exclude(last_activity__in=_REVIEW_ACTIVITIES)
        .values_list("test_run_test_case_id", "session__user_id", "last_activity_at")
        .iterator(chunk_size=_CHUNK_SIZE)
    )

    items = list(latest.items())
    for offset in range(0, len(items), _BATCH_SIZE):
        batch = [
            case_model(pk=case_id, credited_user_id=user_id)
            for case_id, (_at, user_id) in items[offset : offset + _BATCH_SIZE]
        ]
        case_model.objects.bulk_update(batch, ["credited_user"])


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("projects", "0046_run_case_credited_user"),
        ("test_sessions", "0002_needs_retest"),
    ]

    operations = [
        migrations.RunPython(
            backfill_case_credit, migrations.RunPython.noop, elidable=True
        ),
    ]
