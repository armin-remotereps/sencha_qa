from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from test_sessions.services.close_stale_sessions import close_stale_sessions


@shared_task(
    name="test_sessions.tasks.close_stale_test_sessions",
    # Beat (CELERY_BEAT_SCHEDULE); routed to "notify" in CELERY_TASK_ROUTES.
    queue="notify",
    max_retries=0,
    soft_time_limit=120,
    time_limit=150,
)
def close_stale_test_sessions() -> None:
    """End sessions left open past ``TEST_SESSION_MAX_HOURS`` (0: no-op)."""
    close_stale_sessions(timezone.now())
