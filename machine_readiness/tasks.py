"""Celery tasks of machine readiness; thin wrappers around the services."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from machine_readiness.exceptions import Stage0AnalysisBusyError
from machine_readiness.services.analyse_preconditions import (
    CALL_TIMEOUT_SECONDS,
    analyse_preconditions,
)
from machine_readiness.task_names import ANALYSE_PRECONDITIONS

logger = logging.getLogger(__name__)

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
