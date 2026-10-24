from __future__ import annotations

import logging

from celery import shared_task
from celery.app.task import Task

from machine_setup.services.case_auto_fix import (
    start_case_auto_fix as start_case_auto_fix_service,
)
from machine_setup.services.queue_turn import RUN_SETUP_TURN_TASK
from machine_setup.services.run_turn import run_setup_turn as run_setup_turn_service
from machine_setup.services.sweep_stale_auto_fixes import (
    sweep_stale_auto_fixes as sweep_stale_auto_fixes_service,
)
from machine_setup.task_names import START_CASE_AUTO_FIX, SWEEP_STALE_AUTO_FIXES
from machine_setup.turn_state import SETUP_TURN_SOFT_TIME_LIMIT, SETUP_TURN_TIME_LIMIT

logger = logging.getLogger(__name__)

# start_case_auto_fix only creates rows and enqueues the setup turn.
_AUTO_FIX_START_SOFT_TIME_LIMIT = 60
_AUTO_FIX_START_TIME_LIMIT = 90
# The sweep only reads and settles rows; it runs every two minutes.
_SWEEP_SOFT_TIME_LIMIT = 60
_SWEEP_TIME_LIMIT = 90


@shared_task(
    bind=True,
    name=RUN_SETUP_TURN_TASK,
    queue="execution",
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
    # The agent deadline plus headroom for an in-flight tool; a turn still live
    # past the hard limit is treated as stale (machine_setup.turn_state).
    soft_time_limit=SETUP_TURN_SOFT_TIME_LIMIT,
    time_limit=SETUP_TURN_TIME_LIMIT,
)
def run_setup_turn(self: Task[[int], None], turn_id: int) -> None:
    logger.info(
        "run_setup_turn started: task_id=%s turn_id=%s", self.request.id, turn_id
    )
    run_setup_turn_service(turn_id)
    logger.info(
        "run_setup_turn finished: task_id=%s turn_id=%s", self.request.id, turn_id
    )


@shared_task(
    bind=True,
    # Sent by name from projects (machine_setup.task_names).
    name=START_CASE_AUTO_FIX,
    queue="execution",
    max_retries=0,
    # Idempotent (a second delivery finds the thread and does nothing), so a
    # lost worker's task is redelivered rather than leaving the case waiting.
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_AUTO_FIX_START_SOFT_TIME_LIMIT,
    time_limit=_AUTO_FIX_START_TIME_LIMIT,
)
def start_case_auto_fix(self: Task[[int], None], help_request_id: int) -> None:
    logger.info(
        "start_case_auto_fix: task_id=%s help_request_id=%s",
        self.request.id,
        help_request_id,
    )
    start_case_auto_fix_service(help_request_id)


@shared_task(
    bind=True,
    # Scheduled by CELERY_BEAT_SCHEDULE (settings) every two minutes. On the
    # small notify worker so a busy or failing execution pool cannot hold it.
    name=SWEEP_STALE_AUTO_FIXES,
    queue="notify",
    max_retries=0,
    soft_time_limit=_SWEEP_SOFT_TIME_LIMIT,
    time_limit=_SWEEP_TIME_LIMIT,
)
def sweep_stale_auto_fixes(self: Task[[], None]) -> None:
    logger.debug("sweep_stale_auto_fixes: task_id=%s", self.request.id)
    sweep_stale_auto_fixes_service()
