"""The project's Test Runner resolves a help case (Continue, Mark failed / blocked).

The rules of ``runner_person_step_services.answer_from_runner``: only the
connection that owns the project, the shared 20/min runner answer throttle,
the member-or-anonymous actor; then ``case_help_services`` decides.
``find_runner_help_case`` is the stale-card guard shared with discussion
sends (``machine_setup.services.runner_discussion``).
"""

from __future__ import annotations

from dataclasses import dataclass

from controller_client.protocol import PersonHelpActionKind
from projects.case_help_services import (
    HelpActionResult,
    continue_after_help,
    mark_blocked_after_help,
    mark_failed_after_help,
)
from projects.exceptions import PersonStepError, RunHelpError
from projects.models import HelpContinueMode, HelpResolution, TestRunTestCase
from projects.person_step_actor import PersonStepActor
from projects.runner_person_step_services import (
    check_answer_throttle,
    current_member,
    owns_project,
)

_NOT_OWNER = (
    "This Test Runner is no longer the project's connection. Act on Punk Hazard."
)
_STALE_REQUEST = (
    "This help request was already resolved or replaced. Look at the window again."
)


@dataclass(frozen=True)
class RunnerHelpAction:
    """A validated ``person_help_action`` (``RunnerHelpActionForm``)."""

    pivot_id: int
    help_request_id: int
    action: PersonHelpActionKind
    note: str
    # Continue only; None: the stored mode.
    continue_mode: HelpContinueMode | None
    plan_revision_id: int | None
    use_plan_revision: bool


def resolve_help_from_runner(
    *,
    project_id: int,
    channel_name: str,
    person_user_id: int | None,
    action: RunnerHelpAction,
) -> HelpActionResult:
    """Continue, Mark failed or Mark blocked a help case from the Test Runner.

    ``person_user_id``: the member the runner's email named at handshake,
    re-checked here. ``action.help_request_id`` guards a stale card: refused
    unless it is the case's open request. Raises ``RunHelpError`` (user-safe)
    for a lost connection, the throttle, a case not of the project, a stale
    card, or any refusal of ``case_help_services``.
    """
    if not owns_project(project_id, channel_name):
        raise RunHelpError(_NOT_OWNER)
    try:
        check_answer_throttle(project_id)
    except PersonStepError as exc:
        raise RunHelpError(exc.message) from exc
    pivot = find_runner_help_case(
        project_id=project_id,
        pivot_id=action.pivot_id,
        help_request_id=action.help_request_id,
    )
    actor = PersonStepActor.runner(current_member(project_id, person_user_id))
    if action.action is PersonHelpActionKind.CONTINUE:
        return continue_after_help(
            pivot=pivot,
            actor=actor,
            note=action.note,
            plan_revision_id=action.plan_revision_id,
            use_plan_revision=action.use_plan_revision,
            continue_mode=action.continue_mode,
        )
    if action.action is PersonHelpActionKind.MARK_FAILED:
        return mark_failed_after_help(pivot=pivot, actor=actor, note=action.note)
    return mark_blocked_after_help(pivot=pivot, actor=actor, note=action.note)


def find_runner_help_case(
    *, project_id: int, pivot_id: int, help_request_id: int
) -> TestRunTestCase:
    """The project's case whose open help request is ``help_request_id``.

    Shared by help actions and discussion sends: a card the window showed for
    a request that was resolved or replaced since is refused. One query.
    Raises ``RunHelpError`` (user-safe).
    """
    pivot = (
        TestRunTestCase.objects.filter(
            pk=pivot_id,
            test_run__project_id=project_id,
            help_requests__pk=help_request_id,
            help_requests__resolution=HelpResolution.OPEN,
        )
        .only("id", "test_run_id")
        .first()
    )
    if pivot is None:
        raise RunHelpError(_STALE_REQUEST)
    return pivot
