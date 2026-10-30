from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction. If it fails,
    # drop the INVALID index and run the migration again.
    atomic = False

    dependencies = [
        ("projects", "0047_backfill_case_credit"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="casehelprequest",
            index=models.Index(
                fields=["resolved_at"], name="case_help_resolved_at_idx"
            ),
        ),
        AddIndexConcurrently(
            model_name="casepersonstep",
            index=models.Index(
                fields=["reported_at"], name="person_step_reported_at_idx"
            ),
        ),
    ]
