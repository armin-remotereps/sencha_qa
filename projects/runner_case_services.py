"""The project's Test Runner window reads its cases and resolves help (window v2).

Read side (``get_runner_cases``): the cases the window may show beside the
person steps, at most ``PERSON_CASE_STATE_MAX_CASES``:

- the case of an open person step (the step entries point to it);
- the case whose open help request waits for a person: run PAUSED, request
  open, no Night auto-fix running or starting (``discussion`` only);
- the IN_PROGRESS case of a started run, for the Pause button only
  (``pause`` only; it never makes the window show).

Every view carries the test case text, the bounded log tail (cut in SQL, so
the ``logs`` column is never loaded), the pause panel
(``case_pause_services.get_case_pause_panel``) and, for a help case, the help
panel (``case_help_services.get_help_panel``). The discussion part of the
help payload is left empty here: ``machine_setup.services.runner_discussion``
fills it (``projects.runner_window_state`` composes both).

Write side (``resolve_help_from_runner``): the rules of
``runner_person_step_services.answer_from_runner`` (owner connection, the
shared 20/min runner answer throttle, the member-or-anonymous actor), then
``case_help_services`` decides.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q
from django.db.models.functions import Right
from django.urls import reverse

from controller_client.protocol import (
    PERSON_CASE_LOG_TAIL_MAX_CHARS,
    PERSON_CASE_NUMBERED_STEPS_MAX_ITEMS,
    PERSON_CASE_STATE_MAX_CASES,
    PERSON_CASE_TEXT_MAX_CHARS,
    PERSON_HELP_TEXT_MAX_CHARS,
    PERSON_PLAN_DRAFT_MAX_ROWS,
    PERSON_PLAN_DRAFT_TEXT_MAX_CHARS,
    PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS,
    PERSON_STEP_LABEL_MAX_CHARS,
    PERSON_STEP_MAX_BLOCKERS,
    PERSON_STEP_RESULT_MESSAGE_MAX_CHARS,
    PERSON_STEP_URL_MAX_CHARS,
    ContinueModeWire,
    PersonCasePausePayload,
    PersonCaseRole,
    PersonCaseStatePayload,
    PersonCaseStepPayload,
    PersonHelpActionKind,
    PersonHelpPayload,
    PersonPlanDraftPayload,
    PersonPlanDraftRowPayload,
    cut_log_tail,
)
from notifications.services.site_url import absolute_url
from projects.case_help_services import (
    HelpActionResult,
    HelpPanel,
    continue_after_help,
    get_help_panel,
    mark_blocked_after_help,
    mark_failed_after_help,
)
from projects.case_pause_services import CasePausePanel, get_case_pause_panel
from projects.case_step_refs import numbered_case_steps
from projects.exceptions import PersonStepError, RunHelpError
from projects.models import (
    ACTIVE_RUN_STATUSES,
    HelpAutoFixState,
    HelpContinueMode,
    HelpProposalAction,
    HelpResolution,
    Project,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
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

# The annotation holding the last characters of ``logs``.
_LOG_TAIL = "runner_log_tail"
# The case is in the snapshot without a step (help or running).
_EXTRA_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.NEED_HELP,
    TestRunTestCaseStatus.IN_PROGRESS,
)
_ROLE_ORDER: dict[PersonCaseRole, int] = {
    PersonCaseRole.STEP: 0,
    PersonCaseRole.HELP: 1,
    PersonCaseRole.RUNNING: 2,
}


@dataclass(frozen=True)
class RunnerCaseStep:
    """An open person step the snapshot shows: its case and status."""

    pivot_id: int
    # ``PersonStepStatus`` value.
    status: str


@dataclass(frozen=True)
class RunnerCaseView:
    """One case for the window; ``payload.help`` has no discussion yet."""

    payload: PersonCaseStatePayload
    # The open help request waiting for a person (help cases only).
    help_request_id: int | None = None


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


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_runner_cases(
    project_id: int,
    steps: tuple[RunnerCaseStep, ...],
    *,
    discussion: bool,
    pause: bool,
) -> tuple[RunnerCaseView, ...]:
    """The cases the project's runner window may show (≤ 3), steps first.

    ``steps``: the open person steps of the snapshot. ``discussion`` (the
    runner advertised PERSON_DISCUSSION_V1): help cases with their help
    panel; ``pause`` (CASE_PAUSE_V1): the running case and every case's
    pause panel.

    One query for the cases (test case, run and log tail joined in), plus,
    only for a help case, the project and ``get_help_panel``'s queries.
    """
    step_status = {step.pivot_id: step.status for step in steps}
    extra_statuses = [
        status
        for status in _EXTRA_STATUSES
        if (status == TestRunTestCaseStatus.NEED_HELP and discussion)
        or (status == TestRunTestCaseStatus.IN_PROGRESS and pause)
    ]
    match = Q(pk__in=list(step_status))
    if extra_statuses:
        match |= Q(
            test_run__project_id=project_id,
            test_run__status__in=ACTIVE_RUN_STATUSES,
            status__in=extra_statuses,
        )
    if not step_status and not extra_statuses:
        return ()
    pivots = list(
        TestRunTestCase.objects.filter(match, test_run__project_id=project_id)
        .select_related("test_case", "test_run")
        .defer("logs")
        .annotate(**{_LOG_TAIL: Right("logs", PERSON_CASE_LOG_TAIL_MAX_CHARS)})
        .order_by("-updated_at", "-id")[: 2 * PERSON_CASE_STATE_MAX_CASES]
    )
    project: Project | None = None
    views: list[tuple[PersonCaseRole, RunnerCaseView]] = []
    for pivot in pivots:
        role = _role(pivot, step_status)
        if role is None:
            continue
        help_panel: HelpPanel | None = None
        if role is PersonCaseRole.HELP:
            if project is None:
                project = Project.objects.get(pk=project_id)
            help_panel = _waiting_help_panel(pivot, project)
            if help_panel is None:
                continue
        pause_panel = (
            get_case_pause_panel(
                pivot.test_run,
                pivot,
                help_panel=help_panel,
                person_step_status=step_status.get(pivot.pk),
            )
            if pause
            else None
        )
        views.append(
            (role, _build_view(project_id, pivot, role, help_panel, pause_panel))
        )
    views.sort(key=lambda item: _ROLE_ORDER[item[0]])
    return tuple(view for _, view in views[:PERSON_CASE_STATE_MAX_CASES])


def _role(pivot: TestRunTestCase, step_status: dict[int, str]) -> PersonCaseRole | None:
    if pivot.pk in step_status:
        return PersonCaseRole.STEP
    if (
        pivot.status == TestRunTestCaseStatus.NEED_HELP
        and pivot.test_run.status == TestRunStatus.PAUSED
    ):
        return PersonCaseRole.HELP
    if (
        pivot.status == TestRunTestCaseStatus.IN_PROGRESS
        and pivot.test_run.status == TestRunStatus.STARTED
    ):
        return PersonCaseRole.RUNNING
    return None


def _waiting_help_panel(pivot: TestRunTestCase, project: Project) -> HelpPanel | None:
    """The case's help panel while its open request waits for a person."""
    panel = get_help_panel(pivot.test_run, project, pivot_id=pivot.pk)
    if panel is None or not panel.is_open or panel.auto_fix_live:
        return None
    if panel.request.auto_fix_state == HelpAutoFixState.RUNNING:
        # The Night auto-fix is starting: the request is not a person's yet.
        return None
    return panel


def _build_view(
    project_id: int,
    pivot: TestRunTestCase,
    role: PersonCaseRole,
    help_panel: HelpPanel | None,
    pause_panel: CasePausePanel | None,
) -> RunnerCaseView:
    test_case = pivot.test_case
    test_run = pivot.test_run
    path = reverse(
        "projects:test_run_case_detail",
        kwargs={
            "project_id": project_id,
            "test_run_id": test_run.pk,
            "pivot_id": pivot.pk,
        },
    )
    payload = PersonCaseStatePayload(
        pivot_id=pivot.pk,
        role=role,
        run_name=test_run.display_name[:PERSON_STEP_LABEL_MAX_CHARS],
        case_title=test_case.title[:PERSON_STEP_LABEL_MAX_CHARS],
        status_label=pivot.get_status_display()[:PERSON_STEP_LABEL_MAX_CHARS],
        case_url=(absolute_url(path) or "")[:PERSON_STEP_URL_MAX_CHARS],
        preconditions=test_case.preconditions[:PERSON_CASE_TEXT_MAX_CHARS],
        steps_text=test_case.steps[:PERSON_CASE_TEXT_MAX_CHARS],
        expected_text=test_case.expected[:PERSON_CASE_TEXT_MAX_CHARS],
        numbered_steps=tuple(
            PersonCaseStepPayload(
                num=step.num,
                text=step.text[:PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS],
                expected=step.expected[:PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS],
            )
            for step in numbered_case_steps(test_case)[
                :PERSON_CASE_NUMBERED_STEPS_MAX_ITEMS
            ]
        ),
        log_tail=cut_log_tail(str(getattr(pivot, _LOG_TAIL) or "")),
        pause=_pause_payload(pause_panel) if pause_panel is not None else None,
        help=_help_payload(help_panel) if help_panel is not None else None,
    )
    return RunnerCaseView(
        payload=payload,
        help_request_id=help_panel.request.pk if help_panel is not None else None,
    )


def _pause_payload(panel: CasePausePanel) -> PersonCasePausePayload:
    return PersonCasePausePayload(
        can_pause=panel.can_pause,
        pause_blocker=panel.pause_blocker[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
        pause_pending=panel.pause_pending,
        pause_requested_by=panel.pause_requested_by_label[:PERSON_STEP_LABEL_MAX_CHARS],
        paused=panel.paused,
        paused_by=panel.paused_by_label[:PERSON_STEP_LABEL_MAX_CHARS],
        pause_message_state=panel.pause_message_state,
        pause_message_note=panel.pause_message_note[
            :PERSON_STEP_RESULT_MESSAGE_MAX_CHARS
        ],
    )


def _help_payload(panel: HelpPanel) -> PersonHelpPayload:
    """The help part of a case; the discussion fields stay empty here."""
    request = panel.request
    action = request.agent_proposal_action
    agent_mode = request.agent_proposal_continue_mode if action else ""
    draft = panel.pending_draft
    return PersonHelpPayload(
        help_request_id=request.pk,
        step_num=request.step_num,
        reason=request.reason[:PERSON_HELP_TEXT_MAX_CHARS],
        ask=request.ask[:PERSON_HELP_TEXT_MAX_CHARS],
        fault_label=(request.get_fault_display() if request.fault else "")[
            :PERSON_STEP_LABEL_MAX_CHARS
        ],
        paused_by=(request.paused_by_label if request.is_person_pause else "")[
            :PERSON_STEP_LABEL_MAX_CHARS
        ],
        continue_mode=ContinueModeWire(panel.continue_mode.value),
        continue_mode_choices=tuple(
            ContinueModeWire(mode.value) for mode in panel.continue_mode_choices
        ),
        agent_continue_mode=ContinueModeWire(agent_mode) if agent_mode else None,
        can_continue=panel.can_continue,
        blockers=tuple(
            blocker[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS]
            for blocker in panel.blockers[:PERSON_STEP_MAX_BLOCKERS]
        ),
        proposal_label=HelpProposalAction(action).label if action else "",
        proposal_note=(request.agent_proposal_note if action else "")[
            :PERSON_HELP_TEXT_MAX_CHARS
        ],
        plan_draft=(
            PersonPlanDraftPayload(
                revision_id=draft.revision_id,
                reason=draft.reason[:PERSON_HELP_TEXT_MAX_CHARS],
                resume_step=draft.resume_step,
                carried_count=draft.carried_count,
                rows=tuple(
                    PersonPlanDraftRowPayload(
                        num=row.num,
                        description=row.description[:PERSON_PLAN_DRAFT_TEXT_MAX_CHARS],
                        expected_result=row.expected_result[
                            :PERSON_PLAN_DRAFT_TEXT_MAX_CHARS
                        ],
                        carried_from=row.carried_from or 0,
                        state_label=row.state_label[:PERSON_STEP_LABEL_MAX_CHARS],
                    )
                    for row in draft.new_rows[:PERSON_PLAN_DRAFT_MAX_ROWS]
                ),
            )
            if draft is not None
            else None
        ),
        thread_url="",
        turn_live=False,
        can_send=False,
        send_blocker="",
        messages=(),
    )


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


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
