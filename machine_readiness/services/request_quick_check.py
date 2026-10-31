"""Use case: a member asks for a quick check of the machine (Check machine).

The same gates as the check itself (``quick_check.ensure_quick_check_allowed``),
then the per-project check lock, then the Celery task on commit. The task
(``task_names.QUICK_CHECK``, in ``projects.tasks``) claims the lock with the
token and frees it when it ends.
"""

from __future__ import annotations

from machine_readiness import project_locks
from machine_readiness.exceptions import QuickCheckRefusedError
from machine_readiness.locked_task import send_on_commit
from machine_readiness.runner_ports import RunnerPorts
from machine_readiness.services.quick_check import (
    QUICK_CHECK_BUSY_MESSAGE,
    QUICK_CHECK_LOCK_TTL_SECONDS,
    ensure_quick_check_allowed,
)
from machine_readiness.task_names import QUICK_CHECK


def request_quick_check(
    *, project_id: int, run_id: int | None, actor_id: int, ports: RunnerPorts
) -> None:
    """Queue a check (``assist`` queue) after the same gates as the check.

    Takes the project's check lock, handed to the task (``projects.tasks``),
    which frees it. Raises ``QuickCheckRefusedError`` when a gate refuses or
    a check is already queued or running.
    """
    ensure_quick_check_allowed(project_id=project_id, ports=ports)
    lock = project_locks.acquire(
        project_locks.QUICK_CHECK_LOCK_PREFIX,
        project_id,
        ttl=QUICK_CHECK_LOCK_TTL_SECONDS,
    )
    if lock is None:
        raise QuickCheckRefusedError(QUICK_CHECK_BUSY_MESSAGE)
    send_on_commit(QUICK_CHECK, [project_id, run_id, actor_id], lock)
