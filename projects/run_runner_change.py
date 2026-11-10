"""The run page's "Change runner" action (plan Q6).

A Draft run takes the runner (``run_runner_assignment.assign_runner``); a
PAUSED, DONE or CANCELLED run switches to it (``reassign_runner``: owner of
the target only, new execution generation, current-screen continue mode
withheld for older help requests). A STARTED run cannot switch.

A Human in the loop step waiting for a person on the old machine is handed
back to the agent, as a switch away from the mode does
(``person_step_services.hand_back_person_step``), but the run is not queued:
it stays paused until someone presses "Continue run" on the new runner.
"""

from __future__ import annotations

from django.db import transaction

from accounts.models import CustomUser
from projects.models import TestRun, TestRunStatus
from projects.person_step_services import hand_back_person_step
from projects.run_runner_assignment import assign_runner, reassign_runner
from runners.types import RunnerId


def change_run_runner(
    *, test_run: TestRun, runner_id: RunnerId, actor: CustomUser
) -> TestRun:
    """Assign or switch the run to ``actor``'s runner ``runner_id``.

    The switch and the hand-back of a waiting person step commit together:
    ``reassign_runner`` locks the runners, the project and the run, and the
    hand-back runs under those locks (its broadcasts after commit; the run is
    not queued). Raises the ``runners.exceptions.RunnerError`` subclasses of
    the assignment (permission, not found, not ready, held, run not
    assignable).
    """
    if test_run.status == TestRunStatus.WAITING:
        return assign_runner(test_run=test_run, runner_id=runner_id, actor=actor)
    previous = test_run.runner_id
    with transaction.atomic():
        run = reassign_runner(test_run=test_run, runner_id=runner_id, actor=actor)
        if run.runner_id != previous and run.status == TestRunStatus.PAUSED:
            # Locked by ``reassign_runner`` (runners, project, run): the
            # step goes back to the agent; "Continue run" queues it later.
            # The new machine never showed the step's screen: no current-
            # screen continue (cleanup and restore first).
            hand_back_person_step(run, queue=False, keep_screen=False)
    return run
