"""A Test Runner resolves a help case of one of its runs (Continue, Mark
failed / blocked).

The rules of ``runner_person_step_services.answer_from_runner``: only the
connection that owns the runner, a case of a run assigned to the runner, the
shared 20/min per-runner answer throttle, the owner as actor while a writable
member of the run's project (else refused); then ``case_help_services``
decides.
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
    NOT_RUNNER_CONNECTION,
    NOT_RUNNER_MEMBER,
    check_runner_answer_throttle,
    owns_runner,
    runner_actor,
)
from runners.types import RunnerId

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
    runner_id: RunnerId,
    channel_name: str,
    action: RunnerHelpAction,
) -> HelpActionResult:
    """Continue, Mark failed or Mark blocked a help case from the Test Runner.

    ``action.help_request_id`` guards a stale card: refused unless it is the
    case's open request. Raises ``RunHelpError`` (user-safe) for a lost
    connection, the throttle, a case not of a run assigned to the runner, a
    stale card, an owner who is not a writable member of the run's project,
    or any refusal of ``case_help_services``.
    """
    if not owns_runner(runner_id, channel_name):
        raise RunHelpError(NOT_RUNNER_CONNECTION)
    try:
        check_runner_answer_throttle(runner_id)
    except PersonStepError as exc:
        raise RunHelpError(exc.message) from exc
    pivot = find_runner_help_case(
        runner_id=runner_id,
        pivot_id=action.pivot_id,
        help_request_id=action.help_request_id,
    )
    actor = runner_help_actor(runner_id, pivot)
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
    *, runner_id: RunnerId, pivot_id: int, help_request_id: int
) -> TestRunTestCase:
    """The case of a run assigned to the runner whose open help request is
    ``help_request_id`` (its run joined in).

    Shared by help actions and discussion sends: a card the window showed for
    a request that was resolved or replaced since is refused. One query.
    Raises ``RunHelpError`` (user-safe).
    """
    pivot = (
        TestRunTestCase.objects.filter(
            pk=pivot_id,
            test_run__runner_id=runner_id,
            help_requests__pk=help_request_id,
            help_requests__resolution=HelpResolution.OPEN,
        )
        .select_related("test_run")
        .only("id", "test_run_id", "test_run__id", "test_run__project_id")
        .first()
    )
    if pivot is None:
        raise RunHelpError(_STALE_REQUEST)
    return pivot


def runner_help_actor(runner_id: RunnerId, pivot: TestRunTestCase) -> PersonStepActor:
    """The runner's owner as actor on the case's project (``RunHelpError``
    when not a writable member of it). ``pivot.test_run`` must be loaded
    (``find_runner_help_case`` does)."""
    actor = runner_actor(runner_id, pivot.test_run.project_id)
    if actor is None:
        raise RunHelpError(NOT_RUNNER_MEMBER)
    return actor
