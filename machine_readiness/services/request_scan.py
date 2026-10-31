"""Use case: a member asks for a scan of the base directory (Scan button).

The same gates as the scan itself (``scan_artifacts.scan_refusal``), then
the per-project scan lock, then the Celery task on commit. The task
(``task_names.SCAN_ARTIFACTS``, in ``projects.tasks``) claims the lock with
the token and frees it when it ends.
"""

from __future__ import annotations

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


def request_scan(
    *, project_id: int, actor_id: int, ports: RunnerPorts
) -> ScanAvailability:
    """Queue a scan; returns the availability (for the runner OS label).

    Raises ``ArtifactScanUnavailableError`` when a gate refuses or a scan
    of the project is already queued or running.
    """
    availability, refusal = scan_refusal(
        project_id=project_id, ports=ports, during_turn=False
    )
    if refusal is not None:
        raise ArtifactScanUnavailableError(refusal)
    lock = project_locks.acquire(
        project_locks.SCAN_LOCK_PREFIX, project_id, ttl=SCAN_LOCK_TTL_SECONDS
    )
    if lock is None:
        raise ArtifactScanUnavailableError(SCAN_BUSY_MESSAGE)
    send_on_commit(SCAN_ARTIFACTS, [project_id, actor_id], lock)
    return availability
