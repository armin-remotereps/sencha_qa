"""Rename one of the owner's runners."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from accounts.models import CustomUser
from runners.exceptions import (
    RunnerNameTakenError,
    RunnerNotFoundError,
    RunnerPermissionError,
)
from runners.models import TestRunner
from runners.names import normalize_runner_name
from runners.services.runner_access import can_manage
from runners.services.runner_status import broadcast_runner_status
from runners.types import RunnerId


def rename_runner(*, actor: CustomUser, runner_id: RunnerId, name: str) -> TestRunner:
    """Raises ``RunnerNotFoundError`` (missing or archived),
    ``RunnerPermissionError`` (not the owner), ``RunnerNameInvalidError`` or
    ``RunnerNameTakenError``. Runs keep their ``runner_name_snapshot``."""
    clean_name = normalize_runner_name(name)
    try:
        with transaction.atomic():
            runner = (
                TestRunner.objects.select_for_update()
                .filter(pk=runner_id, archived_at__isnull=True)
                .first()
            )
            if runner is None:
                raise RunnerNotFoundError()
            if not can_manage(actor, runner):
                raise RunnerPermissionError()
            if runner.name == clean_name:
                return runner
            taken = (
                TestRunner.objects.filter(
                    owner_id=runner.owner_id,
                    archived_at__isnull=True,
                    name__iexact=clean_name,
                )
                .exclude(pk=runner.pk)
                .exists()
            )
            if taken:
                raise RunnerNameTakenError()
            runner.name = clean_name
            runner.save(update_fields=["name", "updated_at"])
    except IntegrityError as exc:
        raise RunnerNameTakenError() from exc
    transaction.on_commit(lambda: broadcast_runner_status(runner))
    return runner
