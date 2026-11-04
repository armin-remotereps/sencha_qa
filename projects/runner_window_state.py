"""The Test Runner window's snapshots: open person steps, their cases,
discussions, and the Manual run.

Composition only, no rules: ``runner_person_step_services`` gives the steps,
``runner_case_services`` the cases (test case, log tail, pause and help
panels), ``machine_setup.services.runner_discussion`` each help case's
discussion and ``runner_manual_state_services`` the Manual run snapshot. Imported only by ``controller_consumer`` (it sits above both
apps, so ``projects`` services never import it).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from controller_client.protocol import (
    ClientCapability,
    ManualRunStateMessage,
    PersonCaseStatePayload,
    PersonStepStatePayload,
)
from machine_setup.services.runner_discussion import (
    get_runner_discussion,
    get_runner_manual_discussion,
    get_runner_send_blocker,
)
from projects.manual_run_policy import MANUAL_PAUSE_AGENT_BUSY
from projects.models import Project
from projects.runner_case_services import RunnerCaseStep, get_runner_cases
from projects.runner_manual_state_services import build_manual_run_state
from projects.runner_person_step_services import get_runner_person_steps


@dataclass(frozen=True)
class RunnerWindowFeatures:
    """What the runner advertised (all imply PERSON_STEPS_V1)."""

    # PERSON_STEP_CASE_STEPS_V1: the case steps label and covered steps.
    case_steps: bool
    # PERSON_CASE_CONTEXT_V1: ``cases`` at all.
    case_context: bool
    # PERSON_DISCUSSION_V1: help cases and their discussion.
    discussion: bool
    # CASE_PAUSE_V1: the running case and the pause fields.
    pause: bool


@dataclass(frozen=True)
class RunnerWindowState:
    steps: tuple[PersonStepStatePayload, ...]
    # None for a runner without PERSON_CASE_CONTEXT_V1 (no ``cases`` key).
    cases: tuple[PersonCaseStatePayload, ...] | None


def get_runner_window_state(
    project_id: int, features: RunnerWindowFeatures
) -> RunnerWindowState:
    """The snapshot for the project's runner window.

    Without case context this is ``get_runner_person_steps`` alone (as for
    an older runner). Otherwise one more query for the cases, and, only
    while a help case waits for a person, its help panel and discussion.
    """
    views = get_runner_person_steps(project_id, case_steps=features.case_steps)
    steps = tuple(view.to_payload() for view in views)
    if not features.case_context:
        return RunnerWindowState(steps=steps, cases=None)
    runner_cases = get_runner_cases(
        project_id,
        tuple(
            RunnerCaseStep(pivot_id=view.pivot_id, status=view.status.value)
            for view in views
        ),
        discussion=features.discussion,
        pause=features.pause,
    )
    project = runner_cases.project
    send_blocker: str | None = None
    cases: list[PersonCaseStatePayload] = []
    for case_view in runner_cases.views:
        payload = case_view.payload
        if (
            case_view.help_request_id is not None
            and payload.help is not None
            and project is not None
        ):
            if send_blocker is None:
                send_blocker = get_runner_send_blocker(project)
            discussion = get_runner_discussion(
                project, case_view.help_request_id, send_blocker=send_blocker
            )
            payload = replace(
                payload,
                help=replace(
                    payload.help,
                    thread_url=discussion.thread_url,
                    turn_live=discussion.turn_live,
                    can_send=discussion.can_send,
                    send_blocker=discussion.send_blocker,
                    messages=discussion.messages,
                ),
            )
        cases.append(payload)
    return RunnerWindowState(steps=steps, cases=tuple(cases))


def get_runner_manual_state(
    project: Project, capabilities: frozenset[str]
) -> ManualRunStateMessage:
    """The Manual run snapshot for a MANUAL_RUN_V1 runner window.

    The same for every person at the runner: a project's claims all belong to
    its one runner connection. A MANUAL_PAUSE_DISCUSS_V1 window also gets the
    claimed case's discussion (``active`` is the claimed IN_PROGRESS case of
    ``run``, the project's STARTED manual run, so the discussion does not
    re-check the claim).
    """
    state = build_manual_run_state(project=project, runner_capabilities=capabilities)
    if (
        state.active is None
        or state.run is None
        or ClientCapability.MANUAL_PAUSE_DISCUSS_V1.value not in capabilities
    ):
        return state
    discussion = get_runner_manual_discussion(project, state.active.pivot_id)
    active = replace(state.active, discussion=discussion.payload)
    if discussion.setup_turn_live and active.can_continue:
        # Same refusal as ``continue_manual_case``: any setup turn may be
        # using the machine.
        active = replace(
            active, can_continue=False, pause_blocker=MANUAL_PAUSE_AGENT_BUSY
        )
    return replace(state, active=active)
