"""Use case: a member asks for the stage-0 analysis (Analyse button).

The analysis makes LLM calls only, no runner calls, so the only gate is the
project's analysis lock: taken here, handed to the task
(``task_names.ANALYSE_PRECONDITIONS``), which frees it when it ends.
"""

from __future__ import annotations

from machine_readiness import project_locks
from machine_readiness.exceptions import Stage0AnalysisBusyError
from machine_readiness.locked_task import send_on_commit
from machine_readiness.services.analyse_preconditions import (
    ANALYSIS_BUSY_MESSAGE,
    ANALYSIS_LOCK_TTL_SECONDS,
)
from machine_readiness.task_names import ANALYSE_PRECONDITIONS


def request_analysis(*, project_id: int) -> None:
    """Queue the analysis; ``Stage0AnalysisBusyError`` while one is queued
    or running."""
    lock = project_locks.acquire(
        project_locks.ANALYSIS_LOCK_PREFIX,
        project_id,
        ttl=ANALYSIS_LOCK_TTL_SECONDS,
    )
    if lock is None:
        raise Stage0AnalysisBusyError(ANALYSIS_BUSY_MESSAGE)
    # Task args: project_id, max_calls (None = the setting), lock token.
    send_on_commit(ANALYSE_PRECONDITIONS, [project_id, None], lock)
