"""The project's Test Runner window reads its cases (window v2).

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
fills it (``projects.runner_window_state`` composes both). The write side
is ``runner_help_action_services``.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Case, IntegerField, Q, Value, When
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
    PersonHelpPayload,
    PersonPlanDraftPayload,
    PersonPlanDraftRowPayload,
    cut_log_tail,
)
from notifications.services.site_url import absolute_url
from projects.case_help_services import HelpPanel, get_help_panel
from projects.case_pause_services import CasePausePanel, get_case_pause_panel
from projects.case_text_display import case_text_display
from projects.models import (
    HelpAutoFixState,
    HelpProposalAction,
    Project,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)

# The annotation holding the last characters of ``logs``.
_LOG_TAIL = "runner_log_tail"


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
class RunnerCases:
    views: tuple[RunnerCaseView, ...]
    # The project row when a help case needed it (reused by the caller).
    project: Project | None


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_runner_cases(
    project_id: int,
    steps: tuple[RunnerCaseStep, ...],
    *,
    discussion: bool,
    pause: bool,
) -> RunnerCases:
    """The cases the project's runner window may show (≤ 3), steps first.

    ``steps``: the open person steps of the snapshot. ``discussion`` (the
    runner advertised PERSON_DISCUSSION_V1): Need help cases of a paused run
    with their help panel; ``pause`` (CASE_PAUSE_V1): the in-progress case of
    a started run and every case's pause panel.

    One query for the cases (test case, run and log tail joined in; ordered
    step cases, help cases, running cases, so the cap never drops a step's
    case), plus, only for a help case, the project and ``get_help_panel``'s
    queries (one help case at most: a project has one active run).
    """
    step_status = {step.pivot_id: step.status for step in steps}
    match = Q(pk__in=list(step_status))
    if discussion:
        match |= Q(
            test_run__status=TestRunStatus.PAUSED,
            status=TestRunTestCaseStatus.NEED_HELP,
        )
    if pause:
        match |= Q(
            test_run__status=TestRunStatus.STARTED,
            status=TestRunTestCaseStatus.IN_PROGRESS,
        )
    if not step_status and not discussion and not pause:
        return RunnerCases(views=(), project=None)
    pivots = list(
        TestRunTestCase.objects.filter(match, test_run__project_id=project_id)
        .select_related("test_case", "test_run")
        .defer("logs")
        .annotate(
            **{_LOG_TAIL: Right("logs", PERSON_CASE_LOG_TAIL_MAX_CHARS)},
            runner_role_order=Case(
                When(pk__in=list(step_status), then=Value(0)),
                When(status=TestRunTestCaseStatus.NEED_HELP, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            ),
        )
        .order_by("runner_role_order", "-updated_at", "-id")[
            :PERSON_CASE_STATE_MAX_CASES
        ]
    )
    project: Project | None = None
    views: list[RunnerCaseView] = []
    help_seen = False
    for pivot in pivots:
        role = _role(pivot, step_status)
        if role is None:
            continue
        help_panel: HelpPanel | None = None
        if role is PersonCaseRole.HELP:
            if help_seen:
                continue
            help_seen = True
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
        views.append(_build_view(project_id, pivot, role, help_panel, pause_panel))
    return RunnerCases(views=tuple(views), project=project)


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
    case_text = case_text_display(test_case)
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
        preconditions=case_text.preconditions[:PERSON_CASE_TEXT_MAX_CHARS],
        steps_text=case_text.steps_text[:PERSON_CASE_TEXT_MAX_CHARS],
        expected_text=case_text.expected_text[:PERSON_CASE_TEXT_MAX_CHARS],
        numbered_steps=tuple(
            PersonCaseStepPayload(
                num=step.num,
                text=step.text[:PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS],
                expected=step.expected[:PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS],
            )
            for step in case_text.numbered_steps[:PERSON_CASE_NUMBERED_STEPS_MAX_ITEMS]
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
