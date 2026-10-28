from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0035_run_case_status_finished_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="testruntestcase",
            name="segment_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="testruntestcase",
            name="active_duration",
            field=models.DurationField(blank=True, null=True),
        ),
    ]
