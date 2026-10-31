"""Celery tasks of machine readiness; thin wrappers around the services."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from machine_readiness.exceptions import (
    MachineReadinessError,
    Stage0AnalysisBusyError,
)
from machine_readiness.services.analyse_preconditions import (
    CALL_TIMEOUT_SECONDS,
    analyse_preconditions,
)
from machine_readiness.services.quick_check import check_requirements
from machine_readiness.services.scan_artifacts import (
    SCAN_REPLY_TIMEOUT_SECONDS,
    scan_artifacts,
)
from machine_readiness.task_names import (
    ANALYSE_PRECONDITIONS,
    QUICK_CHECK,
    SCAN_ARTIFACTS,
)

logger = logging.getLogger(__name__)

# One runner reply (with a reconnect retry) plus the database writes.
_SCAN_SOFT_LIMIT = int(SCAN_REPLY_TIMEOUT_SECONDS) * 2 + 30
# The check stops itself at its budget; the margin covers the writes.
_QUICK_CHECK_SOFT_LIMIT = int(settings.STAGE0_QUICK_CHECK_BUDGET_SECONDS) + 30

# Every call may take CALL_TIMEOUT_SECONDS; the writes add little.
_ANALYSIS_SOFT_LIMIT = (
    max(int(settings.STAGE0_ANALYSIS_MAX_CALLS), 1) * CALL_TIMEOUT_SECONDS + 60
)


@shared_task(
    name=ANALYSE_PRECONDITIONS,
    max_retries=0,
    soft_time_limit=_ANALYSIS_SOFT_LIMIT,
    time_limit=_ANALYSIS_SOFT_LIMIT + 60,
)
def analyse_preconditions_task(project_id: int, max_calls: int | None = None) -> None:
    """Stage-0 analysis of the project's unanalysed preconditions (``upload``)."""
    limit = settings.STAGE0_ANALYSIS_MAX_CALLS if max_calls is None else max_calls
    try:
        outcome = analyse_preconditions(
            project_id=project_id,
            max_calls=min(limit, settings.STAGE0_ANALYSIS_MAX_CALLS),
        )
    except Stage0AnalysisBusyError:
        logger.info("stage0 analysis already running: project=%s", project_id)
        return
    logger.info(
        "stage0 analysis done: project=%s calls=%d analysed=%d created=%d "
        "remaining=%d problems=%d error=%s",
        project_id,
        outcome.calls,
        outcome.analysed,
        outcome.requirements_created,
        outcome.remaining,
        len(outcome.problems),
        outcome.error or "-",
    )


@shared_task(
    name=SCAN_ARTIFACTS,
    max_retries=0,
    soft_time_limit=_SCAN_SOFT_LIMIT,
    time_limit=_SCAN_SOFT_LIMIT + 30,
)
def scan_artifacts_task(project_id: int, actor_id: int | None) -> None:
    """Inventory of the base directory on the test machine (``assist``)."""
    try:
        outcome = scan_artifacts(project_id=project_id, actor_id=actor_id)
    except MachineReadinessError as exc:
        logger.info("artifact scan not done: project=%s reason=%s", project_id, exc)
        return
    logger.info(
        "artifact scan done: project=%s os=%s entries=%d truncated=%s error=%s",
        project_id,
        outcome.os_name,
        outcome.entry_count,
        outcome.truncated,
        "yes" if outcome.error else "no",
    )


@shared_task(
    name=QUICK_CHECK,
    max_retries=0,
    soft_time_limit=_QUICK_CHECK_SOFT_LIMIT,
    time_limit=_QUICK_CHECK_SOFT_LIMIT + 30,
)
def quick_check_task(project_id: int, run_id: int | None, actor_id: int | None) -> None:
    """Check machine: the stage-0 requirements without an LLM (``assist``).

    The service broadcasts its own result event.
    """
    try:
        check_requirements(project_id=project_id, run_id=run_id, actor_id=actor_id)
    except MachineReadinessError as exc:
        logger.info(
            "quick check not done: project=%s run=%s reason=%s",
            project_id,
            run_id,
            exc,
        )
