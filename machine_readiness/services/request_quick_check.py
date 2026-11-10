"""Use case: a member asks for a quick check of the machine (Check machine).

Under the Test Runner's row lock: the same gates as the check itself
(``quick_check.ensure_quick_check_allowed``), then the runner's check lock,
then the Celery task on commit. The task (``task_names.QUICK_CHECK``, in
``projects.tasks``) claims the lock with the token and frees it when it ends.
"""

from __future__ import annotations

from django.db import transaction

from machine_readiness import project_locks
from machine_readiness.exceptions import QuickCheckRefusedError
from machine_readiness.locked_task import send_on_commit
from machine_readiness.runner_ports import RunnerPorts
from machine_readiness.services.quick_check import (
    QUICK_CHECK_BUSY_MESSAGE,
    QUICK_CHECK_LOCK_TTL_SECONDS,
    ensure_quick_check_allowed,
)
from machine_readiness.services.runner_gates import NO_RUNNER_MESSAGE
from machine_readiness.task_names import QUICK_CHECK
from runners.locks import lock_runner_row
from runners.types import RunnerId


@transaction.atomic
def request_quick_check(
    *,
    project_id: int,
    runner_id: RunnerId | None,
    run_id: int | None,
    actor_id: int,
    ports: RunnerPorts,
) -> None:
    """Queue a check on ``runner_id`` (``assist`` queue) after the same gates
    as the check.

    ``runner_id``: the run's Test Runner (run card). Takes the runner's check
    lock, handed to the task (``projects.tasks``), which frees it. Raises
    ``QuickCheckRefusedError`` when a gate refuses or a check is already
    queued or running on the runner.
    """
    if runner_id is None:
        raise QuickCheckRefusedError(NO_RUNNER_MESSAGE)
    lock_runner_row(runner_id)
    ensure_quick_check_allowed(project_id=project_id, runner_id=runner_id, ports=ports)
    lock = project_locks.acquire(
        project_locks.RUNNER_QUICK_CHECK_LOCK_PREFIX,
        runner_id,
        ttl=QUICK_CHECK_LOCK_TTL_SECONDS,
    )
    if lock is None:
        raise QuickCheckRefusedError(QUICK_CHECK_BUSY_MESSAGE)
    send_on_commit(QUICK_CHECK, [project_id, runner_id, run_id, actor_id], lock)
