"""A Test Runner pauses a case of one of its runs (server-side seam).

The runner window sends a pause (protocol owned by
``docs/plan/feat-runner-person-window-v2.md``); every rule of a pause stays in
``case_pause_services.request_case_pause``. This module applies the rules of
``runner_person_step_services.answer_from_runner``: only the connection that
owns the runner, the same shared per-runner answer throttle (20/min, a pause
counts as an answer), a case of a run assigned to the runner that runs or
waits for a person, and the pauser is the runner's owner ("<owner> via Test
Runner") while a writable member of the run's project (else refused).
"""

from __future__ import annotations

from projects.case_pause_policy import PAUSE_NOT_RUNNING, CasePauseOutcome
from projects.case_pause_services import request_case_pause
from projects.exceptions import CasePauseError, PersonStepError
from projects.models import TestRunTestCase, TestRunTestCaseStatus

# The shared helpers of the runner's answers; the throttle key and limit
# must stay the same for every runner answer.
from projects.runner_person_step_services import (
    NOT_RUNNER_CONNECTION,
    NOT_RUNNER_MEMBER,
    check_runner_answer_throttle,
    owns_runner,
    runner_actor,
)
from runners.types import RunnerId

_PAUSABLE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.IN_PROGRESS,
    TestRunTestCaseStatus.AWAITING_PERSON,
)


def pause_from_runner(
    *,
    runner_id: RunnerId,
    channel_name: str,
    pivot_id: int,
    message: str,
) -> CasePauseOutcome:
    """Pause a running or waiting case of a run assigned to the Test Runner.

    Raises ``CasePauseError`` (user-safe) when the connection no longer owns
    the runner, the runner answered too often, the case is not a running or
    waiting case of one of its runs, the owner is not a writable member of
    the run's project, or ``request_case_pause`` refuses.
    """
    if not owns_runner(runner_id, channel_name):
        raise CasePauseError(NOT_RUNNER_CONNECTION)
    try:
        check_runner_answer_throttle(runner_id)
    except PersonStepError as exc:
        raise CasePauseError(exc.message) from exc
    pivot = (
        TestRunTestCase.objects.filter(
            pk=pivot_id,
            test_run__runner_id=runner_id,
            status__in=_PAUSABLE_STATUSES,
        )
        .select_related("test_run")
        .only("id", "test_run_id", "test_run__id", "test_run__project_id")
        .first()
    )
    if pivot is None:
        raise CasePauseError(PAUSE_NOT_RUNNING)
    actor = runner_actor(runner_id, pivot.test_run.project_id)
    if actor is None:
        raise CasePauseError(NOT_RUNNER_MEMBER)
    return request_case_pause(pivot=pivot, actor=actor, message=message)
