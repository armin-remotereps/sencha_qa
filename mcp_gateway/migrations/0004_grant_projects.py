"""One OAuth connection covers several projects (docs/plan/feat-mcp-multi-project.md).

Copies every grant's project and key fingerprint into a ``McpGrantProject``
link (revoked grants included, so the history keeps the project name), then
drops ``McpGrant.project`` / ``api_key_fingerprint``. Authorization codes in
flight (60 s) are deleted: the user retries the sign-in. The reverse step
only works while every grant has exactly one link.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.utils import timezone


def copy_grants_to_links(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    McpGrant = apps.get_model("mcp_gateway", "McpGrant")
    McpGrantProject = apps.get_model("mcp_gateway", "McpGrantProject")
    McpAuthorizationCode = apps.get_model("mcp_gateway", "McpAuthorizationCode")
    McpAuthorizationCode.objects.all().delete()
    links = [
        McpGrantProject(
            grant_id=grant.pk,
            project_id=grant.project_id,
            api_key_fingerprint=grant.api_key_fingerprint,
            created_at=grant.created_at,
            revoked_at=grant.revoked_at,
            revoked_reason=grant.revoked_reason,
        )
        for grant in McpGrant.objects.all().only(
            "pk",
            "project_id",
            "api_key_fingerprint",
            "created_at",
            "revoked_at",
            "revoked_reason",
        )
    ]
    McpGrantProject.objects.bulk_create(links, batch_size=500)


def copy_links_to_grants(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    McpGrant = apps.get_model("mcp_gateway", "McpGrant")
    McpGrantProject = apps.get_model("mcp_gateway", "McpGrantProject")
    McpAuthorizationCode = apps.get_model("mcp_gateway", "McpAuthorizationCode")
    McpAuthorizationCode.objects.all().delete()
    links: dict[int, list[object]] = {}
    for link in McpGrantProject.objects.all().only(
        "grant_id", "project_id", "api_key_fingerprint"
    ):
        links.setdefault(link.grant_id, []).append(link)
    for grant in McpGrant.objects.all().only("pk"):
        grant_links = links.get(grant.pk, [])
        if len(grant_links) != 1:
            raise RuntimeError(
                f"McpGrant #{grant.pk} has {len(grant_links)} project links; "
                "cannot reverse to one project per grant."
            )
        link = grant_links[0]
        McpGrant.objects.filter(pk=grant.pk).update(
            project_id=link.project_id,
            api_key_fingerprint=link.api_key_fingerprint,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("mcp_gateway", "0003_staged_capture_source"),
        ("projects", "0060_capture_allowlist"),
    ]

    operations = [
        migrations.CreateModel(
            name="McpGrantProject",
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
                ("api_key_fingerprint", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(default=timezone.now)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "revoked_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("user", "Revoked by the user"),
                            ("client", "Revoked by the app"),
                            ("refresh_reuse", "Refresh token reused"),
                            ("code_reuse", "Authorization code reused"),
                            ("membership", "Access to the project ended"),
                            ("removed", "Removed by you"),
                        ],
                        default="",
                        max_length=16,
                    ),
                ),
                (
                    "grant",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="projects_links",
                        to="mcp_gateway.mcpgrant",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="projects.project",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("grant", "project"), name="mcp_grant_project_uniq"
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="mcpauthorizationcode",
            name="project_ids",
            field=models.JSONField(default=list),
        ),
        # Nullable first, so the reverse can re-add them on populated tables
        # before ``copy_links_to_grants`` fills them.
        migrations.AlterField(
            model_name="mcpgrant",
            name="project",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="projects.project",
            ),
        ),
        migrations.AlterField(
            model_name="mcpgrant",
            name="api_key_fingerprint",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="mcpauthorizationcode",
            name="project",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="projects.project",
            ),
        ),
        migrations.RunPython(copy_grants_to_links, copy_links_to_grants),
        migrations.RemoveField(model_name="mcpauthorizationcode", name="project"),
        migrations.RemoveField(model_name="mcpgrant", name="api_key_fingerprint"),
        migrations.RemoveField(model_name="mcpgrant", name="project"),
        migrations.AlterField(
            model_name="mcpgrant",
            name="revoked_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("user", "Revoked by the user"),
                    ("client", "Revoked by the app"),
                    ("refresh_reuse", "Refresh token reused"),
                    ("code_reuse", "Authorization code reused"),
                    ("membership", "Access to the project ended"),
                    ("removed", "Removed by you"),
                ],
                default="",
                max_length=16,
            ),
        ),
    ]
