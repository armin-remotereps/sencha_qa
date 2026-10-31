"""The project's Test Runner reads and answers its open person steps (Human in the loop).

The runner window shows the actionable steps of the project's paused runs
(``get_runner_person_steps``) and sends the person's answer
(``answer_from_runner``). Every rule of an answer stays in
``person_step_services``; this module only scopes the answer to the
connection that owns the project, throttles it and names who answered.

The snapshot carries no reopen blockers: computing them on every push cost
several queries and went stale while the window stayed open. The window keeps
Report enabled, and an answer the gates refuse is still saved; its result
names the blocker ("Saved, but the run cannot continue yet: ...").

Assist (PERSON_STEP_ASSIST_V1): a runner that advertised it gets each step's
"How to do it" list and its Ask AI questions (``ask_from_runner``); an older
runner gets the how-to inlined into the instruction text and no questions.

Case steps (PERSON_STEP_CASE_STEPS_V1): a runner that advertised it gets the
plain "Step N of M" label, the step's ``case_steps_label`` ("TestRail steps
3–5") and the covered test case steps; an older runner gets the label joined
into ``step_label`` ("Step 2 of 4 · TestRail steps 3–5") and no steps.

Identity: the runner may carry a ``person_email``. It counts only when it is
an active member of the runner's project (``resolve_runner_person``, at
handshake) and still is when the answer arrives; otherwise the answer is
anonymous ("a person at the Test Runner"). Nothing here tells the runner
whether the email matched.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from django.core.cache import cache
from django.db.models import Case, CharField, F, Func, IntegerField, Value, When
from django.db.models.fields.json import KeyTransform
from django.urls import reverse

from accounts.models import CustomUser
from agents.services.person_step_prompts import format_how_to_text
from agents.types import PersonHowToItem
from controller_client.protocol import (
    PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS,
    PERSON_STEP_CASE_STEPS_MAX_ITEMS,
    PERSON_STEP_COMMAND_MAX_CHARS,
    PERSON_STEP_EXPECTED_MAX_CHARS,
    PERSON_STEP_HOW_TO_COMMAND_MAX_CHARS,
    PERSON_STEP_HOW_TO_TEXT_MAX_CHARS,
    PERSON_STEP_INSTRUCTION_MAX_CHARS,
    PERSON_STEP_LABEL_MAX_CHARS,
    PERSON_STEP_OBJECTION_MAX_CHARS,
    PERSON_STEP_STATE_MAX_STEPS,
    PERSON_STEP_URL_MAX_CHARS,
    PersonCaseStepPayload,
    PersonHowToPayload,
    PersonStepActionKind,
    PersonStepQuestionAskPayload,
    PersonStepQuestionPayload,
    PersonStepStatePayload,
    PersonStepWireKind,
    PersonStepWireStatus,
)
from notifications.services.site_url import absolute_url
from projects.case_step_refs import CaseStepText, stored_case_steps
from projects.exceptions import PersonStepError, PersonStepQuestionError
from projects.models import (
    WRITABLE_ROLES,
    CasePersonStep,
    PersonStepQuestion,
    PersonStepStatus,
    Project,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_actor import PersonStepActor
from projects.person_step_question_services import (
    ask_person_step_question,
    list_person_step_questions,
)
from projects.person_step_rows import stored_how_to
from projects.person_step_services import (
    PersonStepActionResult,
    mark_blocked_person_step,
    mark_failed_person_step,
    overrule_person_step,
    report_person_step,
)

# Answers one project's Test Runner may send per window (all actions count).
RUNNER_ANSWER_LIMIT = 20
RUNNER_ANSWER_WINDOW_SECONDS = 60

_NOT_OWNER = (
    "This Test Runner is no longer the project's connection. Answer on Punk Hazard."
)
_NOT_OWNER_ASK = (
    "This Test Runner is no longer the project's connection. Ask on Punk Hazard."
)
_THROTTLED = "Too many answers from this Test Runner. Wait a minute and try again."
_STEP_NOT_FOUND = "This step was already answered or is no longer waiting."

_HOW_TO_HEADING = "\n\nHow to do it:\n"
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
    "how_to",
    "case_steps",
    "case_steps_label",
    "test_run_test_case_id",
    "test_run_test_case__test_run_id",
    "test_run_test_case__test_run__name",
    "test_run_test_case__test_case__title",
)


@dataclass(frozen=True)
class RunnerPersonStepView:
    """One open person step as the project's Test Runner window shows it."""

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
    # Assist runners only; inlined into ``instruction`` otherwise.
    how_to: tuple[PersonHowToPayload, ...] = ()
    # Assist runners only: the step's newest questions, oldest first.
    questions: tuple[PersonStepQuestionPayload, ...] = ()
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
            how_to=self.how_to,
            questions=self.questions,
            case_steps_label=self.case_steps_label,
            case_steps=self.case_steps,
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


def get_runner_person_steps(
    project_id: int, *, assist: bool, case_steps: bool
) -> tuple[RunnerPersonStepView, ...]:
    """The actionable person steps of the project's paused runs, newest first (≤ 5).

    One query: the run's name is joined in and the case's checkpoint is never
    loaded (its plan size is read in the database). No reopen blockers.

    ``assist`` (the runner advertised PERSON_STEP_ASSIST_V1): each step carries
    its how-to list and its questions (one more query, only when a step is
    open). Otherwise the how-to is inlined into the instruction text.

    ``case_steps`` (the runner advertised PERSON_STEP_CASE_STEPS_V1): each
    step carries its case steps label and covered steps. Otherwise the label
    is joined into ``step_label``. No extra query either way.
    """
    plan = KeyTransform("plan", "test_run_test_case__execution_checkpoint")
    rows = list(
        CasePersonStep.objects.filter(
            test_run_test_case__test_run__project_id=project_id,
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
    questions: dict[int, tuple[PersonStepQuestionPayload, ...]] = {}
    if assist and rows:
        # ``asked_by`` stays blank: the runner window never shows who asked
        # ("You:"), so member names and emails stay off the runner socket.
        questions = {
            step_id: tuple(replace(view.to_payload(), asked_by="") for view in views)
            for step_id, views in list_person_step_questions(
                [_as_int(row["id"]) for row in rows]
            ).items()
        }
    return tuple(
        _build_view(
            project_id,
            row,
            assist=assist,
            case_steps=case_steps,
            questions=questions,
        )
        for row in rows
    )


def _build_view(
    project_id: int,
    row: dict[str, object],
    *,
    assist: bool,
    case_steps: bool,
    questions: dict[int, tuple[PersonStepQuestionPayload, ...]],
) -> RunnerPersonStepView:
    person_step_id = _as_int(row["id"])
    how_to = stored_how_to(row["how_to"])
    instruction = str(row["instruction"])
    if how_to and not assist:
        instruction += _HOW_TO_HEADING + format_how_to_text(how_to)
    status = PersonStepWireStatus(str(row["status"]))
    kind = PersonStepWireKind(str(row["kind"]))
    pivot_id = _as_int(row["test_run_test_case_id"])
    test_run_id = _as_int(row["test_run_test_case__test_run_id"])
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
        person_step_id=person_step_id,
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
        how_to=_how_to_payloads(how_to) if assist else (),
        questions=questions.get(person_step_id, ()),
        case_steps_label=(
            case_steps_label[:PERSON_STEP_LABEL_MAX_CHARS] if case_steps else ""
        ),
        case_steps=(
            _case_step_payloads(stored_case_steps(row["case_steps"]))
            if case_steps
            else ()
        ),
    )


def _how_to_payloads(
    how_to: Sequence[PersonHowToItem],
) -> tuple[PersonHowToPayload, ...]:
    return tuple(
        PersonHowToPayload(
            text=item.text[:PERSON_STEP_HOW_TO_TEXT_MAX_CHARS],
            command=item.command[:PERSON_STEP_HOW_TO_COMMAND_MAX_CHARS],
        )
        for item in how_to
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
        for step in steps[:PERSON_STEP_CASE_STEPS_MAX_ITEMS]
    )


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    raise TypeError(f"Expected an int, got {type(value).__name__}")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def resolve_runner_person(project_id: int, email: str | None) -> int | None:
    """The id of the active writable project member with ``email``, or None.

    A read-only member's email resolves to None: the runner then answers as
    an anonymous "person at the Test Runner".

    Emails are unique only by exact case: the exact match wins, then a
    case-insensitive one counts only when it is unambiguous (as
    ``project_member_services._find_active_user``). At most two queries.
    """
    if not email:
        return None
    members = CustomUser.objects.filter(
        is_active=True,
        project_memberships__project_id=project_id,
        project_memberships__role__in=WRITABLE_ROLES,
    )
    exact = members.filter(email=email).values_list("pk", flat=True).first()
    if exact is not None:
        return exact
    matches = list(members.filter(email__iexact=email).values_list("pk", flat=True)[:2])
    if len(matches) == 1:
        return matches[0]
    return None


def current_member(project_id: int, user_id: int | None) -> CustomUser | None:
    """The user if still an active writable project member; None otherwise."""
    if user_id is None:
        return None
    return CustomUser.objects.filter(
        pk=user_id,
        is_active=True,
        project_memberships__project_id=project_id,
        project_memberships__role__in=WRITABLE_ROLES,
    ).first()


# ---------------------------------------------------------------------------
# Answer
# ---------------------------------------------------------------------------


def answer_from_runner(
    *,
    project_id: int,
    channel_name: str,
    person_user_id: int | None,
    action: RunnerPersonStepAnswer,
) -> PersonStepActionResult:
    """Apply the Test Runner's answer to one of its project's open person steps.

    ``person_user_id``: the member the runner's email named at handshake
    (``resolve_runner_person``), re-checked here. Raises ``PersonStepError``
    (user-safe) when the connection no longer owns the project, the project's
    runner answered too often, the step is not one of the project's, or
    ``person_step_services`` refuses (stale id or round, run not paused, ...).
    """
    if not owns_project(project_id, channel_name):
        raise PersonStepError(_NOT_OWNER)
    check_answer_throttle(project_id)
    pivot = _step_pivot(project_id, action.person_step_id)
    if pivot is None:
        raise PersonStepError(_STEP_NOT_FOUND)
    actor = PersonStepActor.runner(current_member(project_id, person_user_id))
    return _ACTIONS[action.action](pivot, actor, action)


def owns_project(project_id: int, channel_name: str) -> bool:
    """The connection is still the project's Test Runner (one query)."""
    return bool(channel_name) and (
        Project.objects.filter(pk=project_id, agent_channel_name=channel_name).exists()
    )


def _step_pivot(project_id: int, person_step_id: int) -> TestRunTestCase | None:
    """The run case of one of the project's person steps (one query)."""
    return (
        TestRunTestCase.objects.filter(
            person_steps__pk=person_step_id,
            test_run__project_id=project_id,
        )
        .only("id", "test_run_id")
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


def check_answer_throttle(project_id: int) -> None:
    key = f"projects:runner-person-answer:{project_id}"
    cache.add(key, 0, timeout=RUNNER_ANSWER_WINDOW_SECONDS)
    try:
        attempts = cache.incr(key)
    except ValueError:
        # The key expired between add and incr: this is the window's first answer.
        cache.add(key, 1, timeout=RUNNER_ANSWER_WINDOW_SECONDS)
        attempts = 1
    if attempts > RUNNER_ANSWER_LIMIT:
        raise PersonStepError(_THROTTLED)


# ---------------------------------------------------------------------------
# Ask AI
# ---------------------------------------------------------------------------


def ask_from_runner(
    *,
    project_id: int,
    channel_name: str,
    person_id: int | None,
    question: PersonStepQuestionAskPayload,
) -> PersonStepQuestion:
    """Store the Test Runner's question about one of its project's open steps.

    The same scoping and identity as ``answer_from_runner``: the connection
    must own the project, the step must be one of the project's, and the
    asker is ``person_id`` while still an active writable member, else
    anonymous. Throttled by the shared question throttle (web and runner,
    inside ``ask_person_step_question``), not the answer throttle. Raises
    ``PersonStepQuestionError`` (user-safe).
    """
    if not owns_project(project_id, channel_name):
        raise PersonStepQuestionError(_NOT_OWNER_ASK)
    pivot = _step_pivot(project_id, question.person_step_id)
    if pivot is None:
        raise PersonStepQuestionError(_STEP_NOT_FOUND)
    actor = PersonStepActor.runner(current_member(project_id, person_id))
    return ask_person_step_question(
        pivot=pivot,
        project_id=project_id,
        actor=actor,
        person_step_id=question.person_step_id,
        question=question.question,
        expected_round=question.round,
    )
