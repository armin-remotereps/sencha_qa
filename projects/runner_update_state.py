"""Whether a Test Runner self-update is in flight, and its lazy expiry.

A leaf module (imports only ``projects.models`` and settings) so run start,
resume, machine setup and the update services can gate on it without import
cycles. It must never import ``projects.services``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings

from projects.models import (
    NON_TERMINAL_RUNNER_UPDATE_STATUSES,
    RunnerUpdate,
    RunnerUpdateStatus,
)

# Why runs, resumes and setup turns are refused while an update is in flight.
RUNNER_UPDATE_RUNNING_MESSAGE = (
    "The Test Runner is being updated. Wait for the update to finish."
)


def runner_update_timeout() -> timedelta:
    """How long an update may stay in flight before it counts as expired."""
    return timedelta(seconds=settings.RUNNER_UPDATE_TIMEOUT_SECONDS)


def runner_update_in_progress(project_id: int, now: datetime) -> bool:
    """Whether the project has an in-flight update younger than the timeout."""
    return RunnerUpdate.objects.filter(
        project_id=project_id,
        status__in=NON_TERMINAL_RUNNER_UPDATE_STATUSES,
        created_at__gte=now - runner_update_timeout(),
    ).exists()


def expire_stale_runner_updates(project_id: int, now: datetime) -> int:
    """Mark the project's in-flight updates older than the timeout ``expired``.

    Call it under the project row lock (request and offer services). Returns the
    number of rows expired (0 or 1, given the one-in-flight constraint).
    """
    return RunnerUpdate.objects.filter(
        project_id=project_id,
        status__in=NON_TERMINAL_RUNNER_UPDATE_STATUSES,
        created_at__lt=now - runner_update_timeout(),
    ).update(status=RunnerUpdateStatus.EXPIRED, finished_at=now, updated_at=now)
