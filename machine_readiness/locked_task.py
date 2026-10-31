"""Queue a machine readiness task that holds a per-project lock."""

from __future__ import annotations

from functools import partial

from django.db import transaction

from auto_tester.celery import app as celery_app
from machine_readiness.project_locks import ProjectLock, release


def send_on_commit(task_name: str, args: list[object], lock: ProjectLock) -> None:
    """Send ``task_name`` by name once the transaction commits.

    The task receives the lock's token as its last argument and frees the
    lock when it ends. When the broker refuses the message the lock is freed
    here, so the next request can try again.
    """
    transaction.on_commit(partial(_send, task_name, [*args, lock.token], lock))


def _send(task_name: str, args: list[object], lock: ProjectLock) -> None:
    try:
        celery_app.send_task(task_name, args=args)
    except Exception:
        release(lock)
        raise
