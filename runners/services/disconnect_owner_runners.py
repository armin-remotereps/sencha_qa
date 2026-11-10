"""Use case: a user was deactivated, so their Test Runners stop serving.

``authenticate_runner`` already refuses a runner whose owner is inactive, so
a reconnect fails; this closes the sockets that are live now (4002). Runs are
not aborted here: a STARTED run waits out the reconnect grace and then asks
for help with the runner-lost fault, so a member can move it to another
runner.
"""

from __future__ import annotations

from runners.models import TestRunner
from runners.services.runner_connection import (
    clear_runner_connection,
    send_runner_close,
)
from runners.services.runner_status import broadcast_runner_status


def disconnect_owner_runners(user_id: int) -> int:
    """Close every live socket of the user's runners; returns how many.

    Call after the deactivation committed (``transaction.on_commit``). One
    query for the connected runners, then per runner a clear, a close event
    and a status broadcast (none of which raise).
    """
    runners = list(TestRunner.objects.filter(owner_id=user_id, connected=True))
    for runner in runners:
        clear_runner_connection(runner.runner_id)
        send_runner_close(runner.runner_id)
        runner.refresh_from_db()
        broadcast_runner_status(runner)
    return len(runners)
