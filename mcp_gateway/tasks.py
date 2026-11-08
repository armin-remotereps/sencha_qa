from __future__ import annotations

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from mcp_gateway.limits import MCP_KEPT_SWEEP_SECONDS
from mcp_gateway.services.command_registry import (
    CommandRegistryUnavailable,
    registered_projects,
)
from mcp_gateway.services.oauth_sweep import sweep_mcp_oauth as sweep_mcp_oauth_rows
from mcp_gateway.services.stop_claim_commands import stop_ended_claim_commands
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


@shared_task(
    name="mcp_gateway.tasks.stop_claim_commands",
    # Sent on commit of an MCP hand-in or release; "assist": short test
    # machine round trips (CELERY_TASK_ROUTES).
    queue="assist",
    max_retries=0,
    ignore_result=True,
    soft_time_limit=180,
    time_limit=210,
)
def stop_claim_commands(project_id: int, pivot_id: int) -> None:
    """Stop the commands of the case's ended claim and the project's orphaned
    kept sessions (best effort)."""
    try:
        stop_ended_claim_commands(project_id, pivot_id, include_orphans=True)
    except CommandRegistryUnavailable:
        # The minute sweep tries again.
        logger.warning("mcp claim-end stop skipped project=%s", project_id)


@shared_task(
    name="mcp_gateway.tasks.sweep_ended_claim_commands",
    # Beat every MCP_KEPT_SWEEP_SECONDS (CELERY_BEAT_SCHEDULE); "assist" in
    # CELERY_TASK_ROUTES.
    queue="assist",
    max_retries=0,
    ignore_result=True,
    soft_time_limit=MCP_KEPT_SWEEP_SECONDS * 2,
    time_limit=MCP_KEPT_SWEEP_SECONDS * 2 + 30,
)
def sweep_ended_claim_commands() -> None:
    """Stop the commands of every ended MCP claim (one Redis call when no
    command is registered)."""
    try:
        project_ids = registered_projects()
    except CommandRegistryUnavailable:
        return
    for project_id in project_ids:
        try:
            stop_ended_claim_commands(project_id)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            # One project's fault must not stop the others.
            logger.exception("mcp claim-end sweep failed project=%s", project_id)
