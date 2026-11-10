"""Settle the work of the retired project Test Runners (user-owned runners).

No project runner can be carried over (no owner, no token), so at deploy:

- Started agent runs are cancelled (generation bumped, so queued chain tasks
  stand down); their In progress cases are cancelled. Checkpoints stay, so a
  person Resumes them later on a chosen runner.
- Paused runs stay paused with no runner (Continue asks for one).
- Started Manual runs stay started; claims made at the old runner window are
  released (MCP claims are kept); hand-ins still uploading from the old
  runner's spool are marked incomplete.
- Every ``RunnerUpdate`` row is deleted (they describe the retired runners).

Irreversible in data: the reverse is a no-op.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.db.models import DateTimeField, F, Value
from django.db.models.functions import Greatest
from django.utils import timezone

STARTED = "started"
CANCELLED = "cancelled"
CREATED = "created"
IN_PROGRESS = "in_progress"
MANUAL = "manual"
MANUAL_SOURCE_MCP = "m"
UPLOADING = "uploading"
INCOMPLETE = "incomplete"


def settle_project_runner_work(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    test_run_model = apps.get_model("projects", "TestRun")
    pivot_model = apps.get_model("projects", "TestRunTestCase")
    submission_model = apps.get_model("projects", "ManualCaseSubmission")
    update_model = apps.get_model("projects", "RunnerUpdate")
    now = timezone.now()
    # As ``case_duration_services.close_case_segment``: add the open segment.
    open_segment = F("active_duration") + Greatest(
        Value(now, output_field=DateTimeField()) - F("segment_started_at"),
        Value(timedelta(0)),
    )

    agent_runs = test_run_model.objects.filter(status=STARTED).exclude(help_mode=MANUAL)
    agent_run_ids = list(agent_runs.values_list("pk", flat=True))
    if agent_run_ids:
        running_cases = pivot_model.objects.filter(
            test_run_id__in=agent_run_ids, status=IN_PROGRESS
        )
        running_cases.filter(segment_started_at__isnull=False).update(
            active_duration=open_segment
        )
        running_cases.update(
            status=CANCELLED,
            finished_at=now,
            updated_at=now,
            segment_started_at=None,
            pause_requested_generation=None,
            pause_requested_by=None,
            pause_requested_label="",
            pause_requested_via="",
            pause_requested_at=None,
            pause_message="",
        )
        test_run_model.objects.filter(pk__in=agent_run_ids).update(
            status=CANCELLED,
            finished_at=now,
            execution_generation=F("execution_generation") + 1,
            updated_at=now,
        )

    window_claims = (
        pivot_model.objects.filter(
            test_run__status=STARTED,
            test_run__help_mode=MANUAL,
            status=IN_PROGRESS,
        )
        .exclude(manual_claim_token="")
        .exclude(manual_claim_source=MANUAL_SOURCE_MCP)
    )
    window_claims.filter(segment_started_at__isnull=False).update(
        active_duration=open_segment
    )
    window_claims.update(
        status=CREATED,
        execution_checkpoint={},
        segment_started_at=None,
        manual_claim_token="",
        manual_claim_source="",
        manual_claimed_by=None,
        manual_claimed_label="",
        manual_claimed_at=None,
        manual_paused_at=None,
        manual_paused_by=None,
        manual_paused_label="",
        manual_paused_via="",
        updated_at=now,
    )

    submission_model.objects.filter(upload_state=UPLOADING).exclude(
        source=MANUAL_SOURCE_MCP
    ).update(upload_state=INCOMPLETE)

    update_model.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0061_run_runner_fields"),
    ]

    operations = [
        migrations.RunPython(settle_project_runner_work, migrations.RunPython.noop),
    ]
