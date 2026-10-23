from __future__ import annotations

import logging

from celery import shared_task
from celery.app.task import Task

from machine_setup.services.run_turn import run_setup_turn as run_setup_turn_service
from machine_setup.turn_state import SETUP_TURN_SOFT_TIME_LIMIT, SETUP_TURN_TIME_LIMIT

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="machine_setup.tasks.run_setup_turn",
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
