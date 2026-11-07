from __future__ import annotations

import logging

from celery import shared_task

from mcp_gateway.services.sweep import sweep_mcp_staging

logger = logging.getLogger(__name__)


@shared_task(
    name="mcp_gateway.tasks.sweep_mcp_staged_shots",
    # Beat (CELERY_BEAT_SCHEDULE); routed to "notify" in CELERY_TASK_ROUTES.
    queue="notify",
    max_retries=0,
    soft_time_limit=120,
    time_limit=150,
)
def sweep_mcp_staged_shots() -> None:
    """Delete MCP staging rows of ended claims."""
    deleted = sweep_mcp_staging()
    if deleted:
        logger.info("mcp staging sweep deleted %d shots", deleted)
