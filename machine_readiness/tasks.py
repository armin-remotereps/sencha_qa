"""Celery tasks of machine readiness; thin wrappers around the services.

The scan and the quick check need runner calls and so live in
``projects.tasks`` (see ``machine_readiness.runner_ports``).
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from machine_readiness.exceptions import Stage0AnalysisBusyError
from machine_readiness.services.analyse_preconditions import (
    ANALYSIS_TASK_SOFT_LIMIT_SECONDS,
    analyse_preconditions,
)
from machine_readiness.task_names import ANALYSE_PRECONDITIONS

logger = logging.getLogger(__name__)


@shared_task(
    name=ANALYSE_PRECONDITIONS,
    max_retries=0,
    soft_time_limit=ANALYSIS_TASK_SOFT_LIMIT_SECONDS,
    time_limit=ANALYSIS_TASK_SOFT_LIMIT_SECONDS + 60,
)
def analyse_preconditions_task(
    project_id: int, max_calls: int | None = None, lock_token: str | None = None
) -> None:
    """Stage-0 analysis of the project's unanalysed preconditions (``upload``).

    ``lock_token``: the lock ``request_analysis`` took; the service frees it.
    """
    limit = settings.STAGE0_ANALYSIS_MAX_CALLS if max_calls is None else max_calls
    try:
        outcome = analyse_preconditions(
            project_id=project_id,
            max_calls=min(limit, settings.STAGE0_ANALYSIS_MAX_CALLS),
            lock_token=lock_token,
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
