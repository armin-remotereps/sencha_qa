"""Use case: a member asks for a scan of the base directory (Scan button).

Under the Test Runner's row lock: the same gates as the scan itself
(``scan_artifacts.scan_refusal``), then the runner's scan lock, then the
Celery task on commit. The task (``task_names.SCAN_ARTIFACTS``, in
``projects.tasks``) claims the lock with the token and frees it when it ends.
"""

from __future__ import annotations

from django.db import transaction

from machine_readiness import project_locks
from machine_readiness.exceptions import ArtifactScanUnavailableError
from machine_readiness.locked_task import send_on_commit
from machine_readiness.runner_ports import RunnerPorts
from machine_readiness.services.scan_artifacts import (
    SCAN_BUSY_MESSAGE,
    SCAN_LOCK_TTL_SECONDS,
    ScanAvailability,
    scan_refusal,
)
from machine_readiness.task_names import SCAN_ARTIFACTS
from runners.locks import lock_runner_row
from runners.types import RunnerId


@transaction.atomic
def request_scan(
    *, project_id: int, runner_id: RunnerId | None, actor_id: int, ports: RunnerPorts
) -> ScanAvailability:
    """Queue a scan on ``runner_id``; returns the availability (for the
    runner OS label).

    ``runner_id`` is one the caller may use (``usable_runners`` on the tab).
    Raises ``ArtifactScanUnavailableError`` when a gate refuses or a scan
    on the runner is already queued or running.
    """
    lock_runner_row(runner_id)
    availability, refusal = scan_refusal(
        project_id=project_id, runner_id=runner_id, ports=ports, turn_id=None
    )
    if refusal is not None or runner_id is None:
        raise ArtifactScanUnavailableError(refusal or SCAN_BUSY_MESSAGE)
    lock = project_locks.acquire(
        project_locks.RUNNER_SCAN_LOCK_PREFIX, runner_id, ttl=SCAN_LOCK_TTL_SECONDS
    )
    if lock is None:
        raise ArtifactScanUnavailableError(SCAN_BUSY_MESSAGE)
    send_on_commit(SCAN_ARTIFACTS, [project_id, runner_id, actor_id], lock)
    return availability
