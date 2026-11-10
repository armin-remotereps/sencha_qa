"""End every live setup turn at the switch to user-owned Test Runners.

A queued or running turn was bound to a retired project runner, so it is
closed the way Force stop closes one (Stopped, ``force_stopped``). Workers
restart with the deploy; the auto-fix sweep settles any auto-fix help request
whose turn this closes. Irreversible in data: the reverse is a no-op.
"""

from __future__ import annotations

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.utils import timezone


def end_live_turns(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    turn_model = apps.get_model("machine_setup", "SetupTurn")
    turn_model.objects.filter(status__in=("queued", "running")).update(
        status="stopped",
        ended_reason="force_stopped",
        error_message="",
        finished_at=timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("machine_setup", "0013_setup_runner_fields"),
    ]

    operations = [
        migrations.RunPython(end_live_turns, migrations.RunPython.noop),
    ]
