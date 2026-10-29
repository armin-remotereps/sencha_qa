# Hand-written: Project.members gets an explicit through model that keeps the
# auto-created M2M table and column, so the database is untouched except for the
# new ``role`` column. Do not regenerate with makemigrations: the autodetector
# would drop the table and every membership with it.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0040_person_step_channel"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="ProjectMembership",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        (
                            "project",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="memberships",
                                to="projects.project",
                            ),
                        ),
                        (
                            "user",
                            models.ForeignKey(
                                db_column="customuser_id",
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="project_memberships",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "db_table": "projects_project_members",
                        "unique_together": {("project", "user")},
                    },
                ),
                migrations.AlterField(
                    model_name="project",
                    name="members",
                    field=models.ManyToManyField(
                        blank=True,
                        related_name="projects",
                        through="projects.ProjectMembership",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="projectmembership",
            name="role",
            field=models.CharField(
                choices=[("member", "Member"), ("read_only", "Read-only")],
                default="member",
                max_length=16,
            ),
        ),
    ]
