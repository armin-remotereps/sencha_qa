"""Mint a new token for a runner; the old token stops working at once."""

from __future__ import annotations

from django.db import transaction

from accounts.models import CustomUser
from runners.exceptions import RunnerNotFoundError, RunnerPermissionError
from runners.models import TestRunner
from runners.services.runner_access import can_manage
from runners.services.runner_connection import (
    clear_runner_connection,
    send_runner_close,
)
from runners.services.runner_status import broadcast_runner_status
from runners.tokens import mint_runner_token
from runners.types import RunnerId


def reissue_runner_token(*, actor: CustomUser, runner_id: RunnerId) -> str:
    """Store a new token and return its plaintext (show it once).

    A live socket authenticated with the old token is closed with 4002
    (``runners.channels.FORCE_DISCONNECT_EVENT`` to ``runner_{id}``) after
    commit; its runs are not aborted: a STARTED run waits out the reconnect
    grace, so the owner can install the new token in time. Raises
    ``RunnerNotFoundError`` or ``RunnerPermissionError``.
    """
    token = mint_runner_token()
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
        runner.token_hash = token.token_hash
        runner.token_encrypted = token.token_encrypted
        runner.save(update_fields=["token_hash", "token_encrypted", "updated_at"])
        was_connected = runner.connected
    if was_connected:
        clear_runner_connection(runner.runner_id)
        send_runner_close(runner.runner_id)
        runner.refresh_from_db()
        broadcast_runner_status(runner)
    return token.plaintext
