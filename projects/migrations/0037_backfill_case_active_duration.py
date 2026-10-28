"""Backfill ``active_duration`` for finished cases that provably ran in one segment.

A case ran its current execution in one segment when neither a help request
nor a Resume / Redesign snapshot is newer than its latest Re-run snapshot (or
exists at all, if it was never re-run). ``started_at`` cannot be trusted for
this: before active durations, every pickup (also the one after a help
Continue, which writes no snapshot) reset it, so a help request older than
``started_at`` still means an earlier segment. For single-segment cases,
``finished_at - started_at`` is exact. Everything else stays null
("unknown"). One set-based UPDATE; idempotent (only null rows); reverse is a
no-op.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Exists, F, OuterRef

_FINISHED_STATUSES = ("success", "failed", "blocked", "cancelled")
_RESUME_KINDS = ("resume", "redesign")
_RERUN_KIND = "rerun"


def backfill(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    TestRunTestCase = apps.get_model("projects", "TestRunTestCase")
    TestRunCaseResume = apps.get_model("projects", "TestRunCaseResume")
    CaseHelpRequest = apps.get_model("projects", "CaseHelpRequest")

    rerun_after_help = TestRunCaseResume.objects.filter(
        test_run_test_case=OuterRef(OuterRef("pk")),
        kind=_RERUN_KIND,
        created_at__gt=OuterRef("requested_at"),
    )
    help_since_rerun = CaseHelpRequest.objects.filter(
        test_run_test_case=OuterRef("pk")
    ).filter(~Exists(rerun_after_help))
    rerun_after_resume = TestRunCaseResume.objects.filter(
        test_run_test_case=OuterRef(OuterRef("pk")),
        kind=_RERUN_KIND,
        created_at__gt=OuterRef("created_at"),
    )
    resume_since_rerun = TestRunCaseResume.objects.filter(
        test_run_test_case=OuterRef("pk"), kind__in=_RESUME_KINDS
    ).filter(~Exists(rerun_after_resume))

    TestRunTestCase.objects.filter(
        active_duration__isnull=True,
        status__in=_FINISHED_STATUSES,
        started_at__isnull=False,
        finished_at__isnull=False,
        finished_at__gte=F("started_at"),
    ).filter(~Exists(help_since_rerun), ~Exists(resume_since_rerun)).update(
        active_duration=F("finished_at") - F("started_at")
    )


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0036_case_active_duration"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
