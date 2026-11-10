"""Drop the project Test Runner state; self-updates belong to a runner.

Runs after ``0062_user_runner_deploy_data`` deleted every ``RunnerUpdate`` row,
so ``RunnerUpdate.runner`` can become required without a default.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0062_user_runner_deploy_data"),
        ("runners", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="runnerupdate",
            name="runner_update_one_in_flight",
        ),
        migrations.RemoveIndex(
            model_name="runnerupdate",
            name="runner_update_project_idx",
        ),
        migrations.RemoveField(model_name="runnerupdate", name="project"),
        migrations.AlterField(
            model_name="runnerupdate",
            name="runner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="updates",
                to="runners.testrunner",
            ),
        ),
        migrations.AddIndex(
            model_name="runnerupdate",
            index=models.Index(
                fields=["runner", "-created_at"], name="runner_update_runner_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="runnerupdate",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        ("sent", "preparing", "installing", "starting", "waiting"),
                    )
                ),
                fields=("runner",),
                name="runner_update_one_in_flight_per_runner",
            ),
        ),
        migrations.RemoveField(model_name="project", name="agent_capabilities"),
        migrations.RemoveField(model_name="project", name="agent_channel_name"),
        migrations.RemoveField(model_name="project", name="agent_client_version"),
        migrations.RemoveField(model_name="project", name="agent_connected"),
        migrations.RemoveField(model_name="project", name="agent_device_detail"),
        migrations.RemoveField(model_name="project", name="agent_device_reported_at"),
        migrations.RemoveField(model_name="project", name="agent_device_state"),
        migrations.RemoveField(model_name="project", name="agent_system_info"),
        migrations.RemoveField(model_name="project", name="last_connected_at"),
    ]
