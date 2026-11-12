from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

# Same value as projects.testrail_mapping.LEGACY_COMMENT_HASH.
_LEGACY_COMMENT_HASH = "legacy"


def mark_legacy_comment_hashes(
    apps: Apps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Results pushed before the neutral comment are re-sent only on a status change.

    Their stored hash is of the old comment (header naming the tester and the
    run), which every new comment differs from; without this the next Push
    would add a second TestRail result for every case already pushed.
    """
    test_run_test_case = apps.get_model("projects", "TestRunTestCase")
    test_run_test_case.objects.exclude(testrail_pushed_comment_hash="").update(
        testrail_pushed_comment_hash=_LEGACY_COMMENT_HASH
    )


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0064_case_result_edit"),
    ]

    operations = [
        migrations.RunPython(
            mark_legacy_comment_hashes, migrations.RunPython.noop, elidable=True
        ),
    ]
