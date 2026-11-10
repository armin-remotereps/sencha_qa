"""A Test Runner reads and answers its open person steps (Human in the loop).

The runner window shows the actionable steps of the paused runs assigned to
the runner (``TestRun.runner``), across every project where the runner's
owner is an active writable member (``get_runner_person_steps``), and sends
the person's answer (``answer_from_runner``). Every rule of an answer stays
in ``person_step_services``; this module only scopes the answer to the
connection that owns the runner and to the runner's own runs, throttles it
per runner and names who answered.

The snapshot carries no reopen blockers: computing them on every push cost
several queries and went stale while the window stayed open. The window keeps
Report enabled, and an answer the gates refuse is still saved; its result
names the blocker ("Saved, but the run cannot continue yet: ...").

Case steps (PERSON_STEP_CASE_STEPS_V1): a runner that advertised it gets the
plain "Step N of M" label, the step's ``case_steps_label`` ("TestRail steps
3–5") and the covered test case steps; an older runner gets the label joined
into ``step_label`` ("Step 2 of 4 · TestRail steps 3–5") and no steps.

Identity: the person at the runner is its owner. An answer counts as
"<owner> via Test Runner" while the owner is an active writable member of the
run's project (``runner_actor``); otherwise it is refused. The shared helpers
of every runner answer (``owns_runner``, ``runner_actor``,
``check_runner_answer_throttle``, ``runner_window_project_ids``) live here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from django.core.cache import cache
from django.db.models import Case, CharField, F, Func, IntegerField, Value, When
from django.db.models.fields.json import KeyTransform
from django.urls import reverse

from accounts.models import CustomUser
from controller_client.protocol import (
    PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS,
    PERSON_STEP_CASE_STEPS_MAX_ITEMS,
    PERSON_STEP_COMMAND_MAX_CHARS,
    PERSON_STEP_EXPECTED_MAX_CHARS,
    PERSON_STEP_INSTRUCTION_MAX_CHARS,
    PERSON_STEP_LABEL_MAX_CHARS,
    PERSON_STEP_OBJECTION_MAX_CHARS,
    PERSON_STEP_STATE_MAX_STEPS,
    PERSON_STEP_URL_MAX_CHARS,
    PersonCaseStepPayload,
    PersonStepActionKind,
    PersonStepStatePayload,
    PersonStepWireKind,
    PersonStepWireStatus,
)
from notifications.services.site_url import absolute_url
from projects.case_step_refs import CaseStepText, stored_case_steps
from projects.case_text_display import case_steps_for_display
from projects.exceptions import PersonStepError
from projects.models import (
    WRITABLE_ROLES,
    CasePersonStep,
    PersonStepStatus,
    ProjectMembership,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_actor import PersonStepActor
from projects.person_step_services import (
    PersonStepActionResult,
    mark_blocked_person_step,
    mark_failed_person_step,
    overrule_person_step,
    report_person_step,
)
from runners.models import TestRunner
from runners.types import RunnerId

# Answers one Test Runner may send per window (all actions count); also the
# per-project limit of ``check_answer_throttle`` (MCP).
RUNNER_ANSWER_LIMIT = 20
RUNNER_ANSWER_WINDOW_SECONDS = 60

# Refusals shared by every runner answer (person steps, pause, help, Manual).
NOT_RUNNER_CONNECTION = (
    "This Test Runner connection was replaced or closed. Answer on Punk Hazard."
)
NOT_RUNNER_MEMBER = (
    "The Test Runner's owner is not a member who can write in this project. "
    "Answer on Punk Hazard."
)
_THROTTLED = "Too many answers from this Test Runner. Wait a minute and try again."
_STEP_NOT_FOUND = "This step was already answered or is no longer waiting."

# Joins the case steps label into ``step_label`` for an older runner.
_CASE_STEPS_LABEL_SEPARATOR = " · "

# The statuses a person may act on (as ``person_step_services``).
_ACTIONABLE_STATUSES: tuple[str, ...] = (
    PersonStepStatus.WAITING,
    PersonStepStatus.NOT_CONFIRMED,
)
_STEP_FIELDS: tuple[str, ...] = (
    "id",
    "round",
    "status",
    "step_num",
    "kind",
    "instruction",
    "expected_result",
    "command",
    "shell_label",
    "agent_objection",
    "case_steps",
    "case_steps_label",
    "test_run_test_case_id",
    "test_run_test_case__test_run_id",
    "test_run_test_case__test_run__project_id",
    "test_run_test_case__test_run__name",
    "test_run_test_case__test_case__title",
)


@dataclass(frozen=True)
class RunnerPersonStepView:
    """One open person step as the Test Runner window shows it."""

    person_step_id: int
    test_run_id: int
    pivot_id: int
    round: int
    status: PersonStepWireStatus
    run_name: str
    case_title: str
    # "Step N of M" ("Step N" when the plan size is unknown); for a runner
    # without PERSON_STEP_CASE_STEPS_V1 followed by " · <case_steps_label>".
    step_label: str
    kind: PersonStepWireKind
    instruction: str
    expected_result: str
    command: str
    shell_label: str
    # The agent's reason while ``not_confirmed``; blank otherwise.
    objection: str
    # Always empty (see the module docstring); kept for the wire contract.
    blockers: tuple[str, ...]
    # Absolute case page URL; blank without ``SITE_URL``.
    case_url: str
    # Case steps runners only ("TestRail steps 3–5"; blank when none).
    case_steps_label: str = ""
    # Case steps runners only: the test case steps the step covers.
    case_steps: tuple[PersonCaseStepPayload, ...] = ()

    def to_payload(self) -> PersonStepStatePayload:
        return PersonStepStatePayload(
            person_step_id=self.person_step_id,
            round=self.round,
            status=self.status,
            run_name=self.run_name,
            case_title=self.case_title,
            step_label=self.step_label,
            kind=self.kind,
            instruction=self.instruction,
            expected_result=self.expected_result,
            command=self.command,
            shell_label=self.shell_label,
            objection=self.objection,
            blockers=self.blockers,
            case_url=self.case_url,
            case_steps_label=self.case_steps_label,
            case_steps=self.case_steps,
            pivot_id=self.pivot_id,
        )


@dataclass(frozen=True)
class RunnerPersonStepAnswer:
    """A validated ``person_step_action`` (``RunnerPersonStepActionForm``)."""

    action: PersonStepActionKind
    person_step_id: int
    # The round the person answered; a reopened step refuses an older round.
    round: int
    # ``PersonStepOutcome`` value for ``report``; blank otherwise.
    outcome: str
    note: str
    # Pasted command output (report only); blank otherwise.
    output: str


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def runner_window_project_ids(runner_id: RunnerId) -> tuple[int, ...]:
    """The non-archived projects where the runner's owner is an active
    writable member (one query): the projects whose runs assigned to the
    runner its window shows. Empty for an archived runner."""
    return tuple(
        ProjectMembership.objects.filter(
            user__test_runners__pk=runner_id,
            user__test_runners__archived_at__isnull=True,
            user__is_active=True,
            role__in=WRITABLE_ROLES,
            project__archived=False,
        )
        .order_by("project_id")
        .values_list("project_id", flat=True)
    )


def get_runner_person_steps(
    runner_id: RunnerId, project_ids: Sequence[int], *, case_steps: bool
) -> tuple[RunnerPersonStepView, ...]:
    """The actionable person steps of the paused runs assigned to the runner
    in ``project_ids`` (``runner_window_project_ids``), newest first (≤ 5).

    One query: the run's name is joined in and the case's checkpoint is never
    loaded (its plan size is read in the database). No reopen blockers.

    ``case_steps`` (the runner advertised PERSON_STEP_CASE_STEPS_V1): each
    step carries its case steps label and covered steps. Otherwise the label
    is joined into ``step_label``. No extra query either way.
    """
    if not project_ids:
        return ()
    plan = KeyTransform("plan", "test_run_test_case__execution_checkpoint")
    rows = list(
        CasePersonStep.objects.filter(
            test_run_test_case__test_run__runner_id=runner_id,
            test_run_test_case__test_run__project_id__in=list(project_ids),
            test_run_test_case__test_run__status=TestRunStatus.PAUSED,
            test_run_test_case__status=TestRunTestCaseStatus.AWAITING_PERSON,
            status__in=_ACTIONABLE_STATUSES,
        )
        .alias(plan_type=Func(plan, function="jsonb_typeof", output_field=CharField()))
        .annotate(
            plan_size=Case(
                When(
                    plan_type="array",
                    then=Func(
                        plan, function="jsonb_array_length", output_field=IntegerField()
                    ),
                ),
                default=Value(0),
                output_field=IntegerField(),
            ),
        )
        .order_by(F("requested_at").desc(), F("id").desc())
        .values(*_STEP_FIELDS, "plan_size")[:PERSON_STEP_STATE_MAX_STEPS]
    )
    return tuple(_build_view(row, case_steps=case_steps) for row in rows)


def _build_view(
    row: dict[str, object],
    *,
    case_steps: bool,
) -> RunnerPersonStepView:
    instruction = str(row["instruction"])
    status = PersonStepWireStatus(str(row["status"]))
    kind = PersonStepWireKind(str(row["kind"]))
    pivot_id = _as_int(row["test_run_test_case_id"])
    test_run_id = _as_int(row["test_run_test_case__test_run_id"])
    project_id = _as_int(row["test_run_test_case__test_run__project_id"])
    # As ``TestRun.display_name``.
    run_name = str(row["test_run_test_case__test_run__name"] or "") or (
        f"Run #{test_run_id}"
    )
    step_num = _as_int(row["step_num"])
    plan_size = _as_int(row["plan_size"])
    step_label = (
        f"Step {step_num} of {plan_size}"
        if plan_size >= step_num
        else f"Step {step_num}"
    )
    case_steps_label = str(row["case_steps_label"] or "")
    if case_steps_label and not case_steps:
        step_label += _CASE_STEPS_LABEL_SEPARATOR + case_steps_label
    path = reverse(
        "projects:test_run_case_detail",
        kwargs={
            "project_id": project_id,
            "test_run_id": test_run_id,
            "pivot_id": pivot_id,
        },
    )
    objection = (
        str(row["agent_objection"])
        if status is PersonStepWireStatus.NOT_CONFIRMED
        else ""
    )
    return RunnerPersonStepView(
        person_step_id=_as_int(row["id"]),
        test_run_id=test_run_id,
        pivot_id=pivot_id,
        round=_as_int(row["round"]),
        status=status,
        run_name=run_name[:PERSON_STEP_LABEL_MAX_CHARS],
        case_title=str(row["test_run_test_case__test_case__title"])[
            :PERSON_STEP_LABEL_MAX_CHARS
        ],
        step_label=step_label[:PERSON_STEP_LABEL_MAX_CHARS],
        kind=kind,
        instruction=instruction[:PERSON_STEP_INSTRUCTION_MAX_CHARS],
        expected_result=str(row["expected_result"])[:PERSON_STEP_EXPECTED_MAX_CHARS],
        command=str(row["command"])[:PERSON_STEP_COMMAND_MAX_CHARS],
        shell_label=str(row["shell_label"])[:PERSON_STEP_LABEL_MAX_CHARS],
        objection=objection[:PERSON_STEP_OBJECTION_MAX_CHARS],
        blockers=(),
        case_url=(absolute_url(path) or "")[:PERSON_STEP_URL_MAX_CHARS],
        case_steps_label=(
            case_steps_label[:PERSON_STEP_LABEL_MAX_CHARS] if case_steps else ""
        ),
        case_steps=(
            _case_step_payloads(stored_case_steps(row["case_steps"]))
            if case_steps
            else ()
        ),
    )


def _case_step_payloads(
    steps: Sequence[CaseStepText],
) -> tuple[PersonCaseStepPayload, ...]:
    return tuple(
        PersonCaseStepPayload(
            num=step.num,
            text=step.text[:PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS],
            expected=step.expected[:PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS],
        )
        for step in case_steps_for_display(steps[:PERSON_STEP_CASE_STEPS_MAX_ITEMS])
    )


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    raise TypeError(f"Expected an int, got {type(value).__name__}")


# ---------------------------------------------------------------------------
# Identity and scope (shared by every runner answer)
# ---------------------------------------------------------------------------


def owns_runner(runner_id: RunnerId, channel_name: str) -> bool:
    """The connection is still the runner's live socket (one query)."""
    return bool(channel_name) and (
        TestRunner.objects.filter(
            pk=runner_id,
            channel_name=channel_name,
            connected=True,
            archived_at__isnull=True,
        ).exists()
    )


def runner_owner_member(runner_id: RunnerId, project_id: int) -> CustomUser | None:
    """The runner's owner while an active writable member of the project
    (one query); None otherwise."""
    return CustomUser.objects.filter(
        test_runners__pk=runner_id,
        is_active=True,
        project_memberships__project_id=project_id,
        project_memberships__role__in=WRITABLE_ROLES,
    ).first()


def runner_actor(runner_id: RunnerId, project_id: int) -> PersonStepActor | None:
    """The actor "<owner> via Test Runner" for an answer about the project,
    or None when the owner is not an active writable member of it (the
    answer is refused then)."""
    owner = runner_owner_member(runner_id, project_id)
    return PersonStepActor.runner(owner) if owner is not None else None


# ---------------------------------------------------------------------------
# Answer
# ---------------------------------------------------------------------------


def answer_from_runner(
    *,
    runner_id: RunnerId,
    channel_name: str,
    action: RunnerPersonStepAnswer,
) -> PersonStepActionResult:
    """Apply the Test Runner's answer to an open person step of one of its runs.

    Raises ``PersonStepError`` (user-safe) when the connection no longer owns
    the runner, the runner answered too often, the step is not one of a run
    assigned to the runner, the owner is not a writable member of the run's
    project, or ``person_step_services`` refuses (stale id or round, run not
    paused, ...).
    """
    if not owns_runner(runner_id, channel_name):
        raise PersonStepError(NOT_RUNNER_CONNECTION)
    check_runner_answer_throttle(runner_id)
    pivot = _step_pivot(runner_id, action.person_step_id)
    if pivot is None:
        raise PersonStepError(_STEP_NOT_FOUND)
    actor = runner_actor(runner_id, pivot.test_run.project_id)
    if actor is None:
        raise PersonStepError(NOT_RUNNER_MEMBER)
    return _ACTIONS[action.action](pivot, actor, action)


def _step_pivot(runner_id: RunnerId, person_step_id: int) -> TestRunTestCase | None:
    """The run case of a person step of a run assigned to the runner (one query)."""
    return (
        TestRunTestCase.objects.filter(
            person_steps__pk=person_step_id,
            test_run__runner_id=runner_id,
        )
        .select_related("test_run")
        .only("id", "test_run_id", "test_run__id", "test_run__project_id")
        .first()
    )


def _report(
    pivot: TestRunTestCase, actor: PersonStepActor, action: RunnerPersonStepAnswer
) -> PersonStepActionResult:
    return report_person_step(
        pivot=pivot,
        actor=actor,
        person_step_id=action.person_step_id,
        outcome=action.outcome,
        note=action.note,
        output=action.output,
        expected_round=action.round,
    )


def _overrule(
    pivot: TestRunTestCase, actor: PersonStepActor, action: RunnerPersonStepAnswer
) -> PersonStepActionResult:
    return overrule_person_step(
        pivot=pivot,
        actor=actor,
        person_step_id=action.person_step_id,
        note=action.note,
        expected_round=action.round,
    )


def _fail(
    pivot: TestRunTestCase, actor: PersonStepActor, action: RunnerPersonStepAnswer
) -> PersonStepActionResult:
    return mark_failed_person_step(
        pivot=pivot,
        actor=actor,
        person_step_id=action.person_step_id,
        note=action.note,
        output=action.output,
        expected_round=action.round,
    )


def _block(
    pivot: TestRunTestCase, actor: PersonStepActor, action: RunnerPersonStepAnswer
) -> PersonStepActionResult:
    return mark_blocked_person_step(
        pivot=pivot,
        actor=actor,
        person_step_id=action.person_step_id,
        note=action.note,
        output=action.output,
        expected_round=action.round,
    )


_ACTIONS: dict[
    PersonStepActionKind,
    Callable[
        [TestRunTestCase, PersonStepActor, RunnerPersonStepAnswer],
        PersonStepActionResult,
    ],
] = {
    PersonStepActionKind.REPORT: _report,
    PersonStepActionKind.OVERRULE: _overrule,
    PersonStepActionKind.FAIL: _fail,
    PersonStepActionKind.BLOCK: _block,
}


def check_runner_answer_throttle(runner_id: RunnerId) -> None:
    """Count one answer of the runner; ``PersonStepError`` past the limit.

    Shared by every runner answer (person steps, pause, help, Manual)."""
    if not hit_runner_throttle(
        f"projects:runner-answer:{runner_id}",
        limit=RUNNER_ANSWER_LIMIT,
        window_seconds=RUNNER_ANSWER_WINDOW_SECONDS,
    ):
        raise PersonStepError(_THROTTLED)


def check_answer_throttle(project_id: int) -> None:
    """The per-project answer throttle (the MCP gateway's tester actions)."""
    if not hit_runner_throttle(
        f"projects:runner-person-answer:{project_id}",
        limit=RUNNER_ANSWER_LIMIT,
        window_seconds=RUNNER_ANSWER_WINDOW_SECONDS,
    ):
        raise PersonStepError(_THROTTLED)


def hit_runner_throttle(key: str, *, limit: int, window_seconds: int) -> bool:
    """Count one attempt under ``key``; False once ``limit`` is exceeded."""
    cache.add(key, 0, timeout=window_seconds)
    try:
        attempts = cache.incr(key)
    except ValueError:
        # The key expired between add and incr: this is the window's first one.
        cache.add(key, 1, timeout=window_seconds)
        attempts = 1
    return attempts <= limit
