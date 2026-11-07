from __future__ import annotations

import logging

from celery import shared_task

from mcp_gateway.services.oauth_sweep import sweep_mcp_oauth as sweep_mcp_oauth_rows
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


@shared_task(
    name="mcp_gateway.tasks.sweep_mcp_oauth",
    # Beat (CELERY_BEAT_SCHEDULE); routed to "notify" in CELERY_TASK_ROUTES.
    queue="notify",
    max_retries=0,
    soft_time_limit=120,
    time_limit=150,
)
def sweep_mcp_oauth() -> None:
    """Delete expired and dead MCP OAuth rows."""
    result = sweep_mcp_oauth_rows()
    if result.tokens or result.grants or result.clients:
        logger.info(
            "mcp oauth sweep deleted tokens=%d grants=%d clients=%d",
            result.tokens,
            result.grants,
            result.clients,
        )
