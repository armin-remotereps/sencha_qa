"""Archive (soft-delete) one of the owner's runners."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.models import ACTIVE_RUN_STATUSES, TestRun
from projects.runner_use import runner_hold
from runners.exceptions import (
    RunnerAssignedError,
    RunnerHeldError,
    RunnerNotFoundError,
    RunnerPermissionError,
)
from runners.locks import lock_runner_row
from runners.models import TestRunner
from runners.services.runner_access import can_manage
from runners.services.runner_connection import (
    clear_runner_connection,
    send_runner_close,
)
from runners.services.runner_status import broadcast_runner_status
from runners.types import RunnerId


def archive_runner(*, actor: CustomUser, runner_id: RunnerId) -> TestRunner:
    """Archive the runner and close its socket.

    Refused with ``RunnerHeldError`` while a holder uses it
    (``projects.runner_use.runner_hold``) and with ``RunnerAssignedError``
    while an active run (STARTED or PAUSED, Manual included) is assigned to
    it. Its token stops authenticating; finished runs keep their name
    snapshot. Raises ``RunnerNotFoundError`` or ``RunnerPermissionError``.
    """
    with transaction.atomic():
        runner = lock_runner_row(runner_id)
        if runner is None or runner.archived_at is not None:
            raise RunnerNotFoundError()
        if not can_manage(actor, runner):
            raise RunnerPermissionError()
        hold = runner_hold(runner.runner_id)
        if hold is not None:
            raise RunnerHeldError(hold.message)
        if TestRun.objects.filter(
            runner_id=runner.pk, status__in=ACTIVE_RUN_STATUSES
        ).exists():
            raise RunnerAssignedError()
        runner.archived_at = timezone.now()
        runner.save(update_fields=["archived_at", "updated_at"])
        was_connected = runner.connected
    if was_connected:
        clear_runner_connection(runner.runner_id)
        send_runner_close(runner.runner_id)
        runner.refresh_from_db()
    broadcast_runner_status(runner)
    return runner
