from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction. If it fails,
    # drop the INVALID index and run the migration again.
    atomic = False

    dependencies = [
        ("projects", "0034_run_case_failed_by"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="testruntestcase",
            index=models.Index(
                fields=["status", "-finished_at"], name="run_case_status_finished_idx"
            ),
        ),
    ]
