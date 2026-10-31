"""The project's Test Runner pauses one of its project's cases (server-side seam).

The runner window sends a pause (protocol owned by
``docs/plan/feat-runner-person-window-v2.md``); every rule of a pause stays in
``case_pause_services.request_case_pause``. This module applies the rules of
``runner_person_step_services.answer_from_runner``: only the connection that
owns the project, the same shared per-project runner answer throttle
(20/min, a pause counts as an answer), a case of that project that runs or
waits for a person, and the pauser named only while the runner's email is
an active writable member (else anonymous: the pause lands, but its first
message is not auto-sent, since a help-chat turn needs a member).
"""

from __future__ import annotations

from projects.case_pause_policy import PAUSE_NOT_RUNNING, CasePauseOutcome
from projects.case_pause_services import request_case_pause
from projects.exceptions import CasePauseError, PersonStepError
from projects.models import TestRunTestCase, TestRunTestCaseStatus
from projects.person_step_actor import PersonStepActor

# The shared helpers of the runner's answers; the throttle key and limit
# must stay the same for every runner answer of a project.
from projects.runner_person_step_services import (
    check_answer_throttle,
    current_member,
    owns_project,
)

_NOT_OWNER = (
    "This Test Runner is no longer the project's connection. Pause on Punk Hazard."
)
_PAUSABLE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.IN_PROGRESS,
    TestRunTestCaseStatus.AWAITING_PERSON,
)


def pause_from_runner(
    *,
    project_id: int,
    channel_name: str,
    person_user_id: int | None,
    pivot_id: int,
    message: str,
) -> CasePauseOutcome:
    """Pause one of the project's running or waiting cases from its Test Runner.

    ``person_user_id``: the member the runner's email named at handshake
    (``resolve_runner_person``), re-checked here. Raises ``CasePauseError``
    (user-safe) when the connection no longer owns the project, the
    project's runner answered too often, the case is not a running or
    waiting case of the project, or ``request_case_pause`` refuses.
    """
    if not owns_project(project_id, channel_name):
        raise CasePauseError(_NOT_OWNER)
    try:
        check_answer_throttle(project_id)
    except PersonStepError as exc:
        raise CasePauseError(exc.message) from exc
    pivot = (
        TestRunTestCase.objects.filter(
            pk=pivot_id,
            test_run__project_id=project_id,
            status__in=_PAUSABLE_STATUSES,
        )
        .only("id", "test_run_id")
        .first()
    )
    if pivot is None:
        raise CasePauseError(PAUSE_NOT_RUNNING)
    actor = PersonStepActor.runner(current_member(project_id, person_user_id))
    return request_case_pause(pivot=pivot, actor=actor, message=message)
