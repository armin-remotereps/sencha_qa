"""Use cases of a person step in a Human in the loop run: report, overrule,
mark failed, mark blocked, mark for re-test, and the hand-back when the run
leaves the mode.

Imports ``run_reopen_services`` and ``services``, never ``machine_setup.services``.

Every action locks the project, then the run, then the person step, then the
case (the order every help writer uses), requires the run to be PAUSED and the
step to be open with the id the card was rendered for (stale-card guard; Mark
for re-test finds the case's open step itself). The
remaining cases are then queued (the reported case first) through
``run_reopen_services.queue_pending_cases`` inside a savepoint: a gate refusal
keeps the report and leaves the run paused with a "Continue run" fallback.

The agent's confirmation is written by the worker (``projects.services``), not
here.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from agents.checkpoint import (
    read_checkpoint_person_step,
    with_person_step,
    without_person_step,
)
from agents.types import (
    CheckpointPersonStep,
    PersonHowToItem,
    PersonMarkVerdict,
    PersonReportOutcome,
    PersonStepHandOff,
)
from controller_client.protocol import ClientCapability
from notifications.services.inbox_broadcast import broadcast_project_attention_changed
from projects.case_credit import credit_cases
from projects.case_result_text import person_label
from projects.case_retest_marking import RETEST_MARK_FIELDS, apply_retest_mark
from projects.case_step_refs import CaseStepText, stored_case_steps
from projects.exceptions import PersonStepError, RunHelpError
from projects.models import (
    ACTIVE_RUN_STATUSES,
    HELP_NOTE_MAX_LENGTH,
    OPEN_PERSON_STEP_STATUSES,
    PERSON_STEP_OUTPUT_MAX_LENGTH,
    RETEST_NOTE_MAX_LENGTH,
    CasePersonStep,
    PersonStepChannel,
    PersonStepClose,
    PersonStepOutcome,
    PersonStepStatus,
    Project,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_mark_verdict import (
    close_marked_row,
    log_no_evidence,
    write_person_mark_verdict,
)
from projects.person_step_actor import PersonStepActor
from projects.person_step_question_services import (
    PersonStepAskState,
    get_person_step_ask_state,
)
from projects.person_step_question_view import PersonStepQuestionView
from projects.person_step_rows import stored_hand_off_kind, stored_how_to
from projects.run_reopen_services import (
    list_wake_blockers,
    lock_project_and_run,
    queue_pending_cases,
)
from projects.services import broadcast_run_and_pivots
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

_NOT_PAUSED = "This run is not waiting for a person."
_STEP_GONE = "This step was already answered or is no longer waiting."
_NOT_CONFIRMED_ONLY = "Only a step the agent could not confirm can be overruled."
_NOTE_REQUIRED = "Describe what was different."
_UNKNOWN_OUTCOME = "Choose how the step went."
_SIGNED_IN_ONLY = "Only a signed-in member can mark a case for re-test."

# The person's turn: the statuses a person may act on.
_ACTIONABLE_STATUSES: tuple[str, ...] = (
    PersonStepStatus.WAITING,
    PersonStepStatus.NOT_CONFIRMED,
)
_OUTPUT_OMITTED = "\n… output shortened …\n"


@dataclass(frozen=True)
class PersonStepPanel:
    """The person step of a paused run, for its case card and run banner."""

    # The open (or latest) step, with ``test_run_test_case__test_case`` loaded.
    step: CasePersonStep
    # "Step N of M" on the case page; "Step N" on the run page (the case's
    # checkpoint, which holds the plan, is not loaded there).
    step_label: str
    # The step waits for the person (``waiting`` or ``not_confirmed``).
    is_open: bool
    # The agent is confirming the person's report (``reported``).
    confirming: bool
    # "failed" / "blocked" while the agent records evidence of the person's
    # mark before that verdict is written (``reported`` with a mark); "" else.
    marked_verdict: str
    # The agent's reason while ``not_confirmed``; empty otherwise.
    objection: str
    # Where to run a command step, e.g. "Terminal (bash)"; empty otherwise.
    shell_label: str
    # The person can report now (open and no gate refuses).
    can_report: bool
    # Why the run cannot be queued right now (runner offline, ...).
    blockers: tuple[str, ...]
    # The open step can also be answered in the Test Runner's window: the
    # project's runner is connected and advertised ``person_steps_v1``.
    runner_can_answer: bool
    # The step's report came from the Test Runner window.
    reported_at_runner: bool
    # The member who reported the step (name, else email); blank before a
    # report and for an anonymous Test Runner report. Never runner-sent text.
    reporter_label: str
    # The model-written "How to do it" items (commands are AI suggestions).
    how_to: tuple[PersonHowToItem, ...]
    # "TestRail steps 3–5" / "Case step 2"; "" when the step covers none.
    case_steps_label: str
    # The covered case steps with their texts (from the row; no query).
    case_steps: tuple[CaseStepText, ...]
    # The step has a command to run (case-stated or suggested): the report
    # form asks for the pasted output.
    has_commands: bool
    # Ask AI: the newest questions of the step, oldest first.
    questions: tuple[PersonStepQuestionView, ...]
    # A person may ask the AI about the step now.
    can_ask: bool
    # Why not (user-safe); "" when ``can_ask``.
    ask_blocker: str
    # A fresh pending question is the only ask blocker (False without questions).
    has_pending: bool

    @property
    def holds_run(self) -> bool:
        """The step, not a Need help of its case, is what the paused run waits on.

        A fault while confirming a report opens a help request: that one wins
        (``case_help_state.get_run_help_state``). A run has at most one open
        step, so this answers that function's person-step check.
        """
        return self.step.test_run_test_case.status != TestRunTestCaseStatus.NEED_HELP


@dataclass(frozen=True)
class PersonStepActionResult:
    # The pending cases are queued: the run continues now.
    queued: bool
    # User-safe reason when a gate refused and the run stays paused.
    blocker: str = ""
    # Mark failed / blocked written at once, without the agent's evidence
    # (why); "" otherwise.
    no_evidence: str = ""


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_person_step_panel(
    test_run: TestRun,
    project: Project,
    *,
    pivot: TestRunTestCase | None = None,
    blockers: Sequence[str] | None = None,
    include_questions: bool = False,
) -> PersonStepPanel | None:
    """The person step panel of an active run; None (no queries) otherwise.

    The run's open step (``waiting``, ``not_confirmed``, or ``reported`` while
    the agent confirms it, which also happens after the run was re-queued).
    One query (with the reporter joined), plus the wake gates while the run
    is PAUSED unless the caller passes them as ``blockers``
    (``list_wake_blockers``). With ``include_questions`` (the case card; the
    run banner never shows Ask AI) one more for the step's questions;
    without, ``questions`` is empty and ``can_ask`` False.

    With ``pivot`` (the case page, which already loaded it with its checkpoint
    and test case): only that case's step, attached to ``pivot``, labelled
    "Step N of M". Without (the run page) the case's checkpoint is not loaded,
    so the label is "Step N".
    """
    if test_run.status not in ACTIVE_RUN_STATUSES:
        return None
    steps = CasePersonStep.objects.filter(
        status__in=OPEN_PERSON_STEP_STATUSES
    ).select_related("reported_by")
    if pivot is not None:
        steps = steps.filter(test_run_test_case_id=pivot.pk)
    else:
        steps = (
            steps.filter(test_run_test_case__test_run_id=test_run.pk)
            .select_related("test_run_test_case__test_case")
            .defer(
                "test_run_test_case__logs",
                "test_run_test_case__execution_checkpoint",
            )
        )
    step = steps.order_by("-id").first()
    if step is None:
        return None
    if pivot is not None:
        step.test_run_test_case = pivot
        step_label = _step_label(step.step_num, pivot.execution_checkpoint)
    else:
        step_label = f"Step {step.step_num}"
    paused = test_run.status == TestRunStatus.PAUSED
    is_open = step.status in _ACTIONABLE_STATUSES
    if not paused:
        blockers = ()
    elif blockers is None:
        blockers = list_wake_blockers(test_run, project)
    not_confirmed = step.status == PersonStepStatus.NOT_CONFIRMED
    how_to = stored_how_to(step.how_to)
    ask = (
        get_person_step_ask_state(step, run_paused=paused)
        if include_questions
        else PersonStepAskState(
            questions=(), can_ask=False, ask_blocker="", has_pending=False
        )
    )
    return PersonStepPanel(
        step=step,
        step_label=step_label,
        is_open=is_open,
        confirming=step.status == PersonStepStatus.REPORTED,
        marked_verdict=_marked_verdict(step),
        objection=step.agent_objection if not_confirmed else "",
        shell_label=step.shell_label,
        can_report=paused and is_open and not blockers,
        blockers=tuple(blockers),
        runner_can_answer=paused and is_open and _runner_shows_steps(project),
        reported_at_runner=step.reported_via == PersonStepChannel.RUNNER,
        reporter_label=person_label(step.reported_by),
        how_to=how_to,
        case_steps_label=step.case_steps_label,
        case_steps=stored_case_steps(step.case_steps),
        has_commands=bool(step.command) or any(item.command for item in how_to),
        questions=ask.questions,
        can_ask=ask.can_ask,
        ask_blocker=ask.ask_blocker,
        has_pending=ask.has_pending,
    )


# ---------------------------------------------------------------------------
# A person's actions
# ---------------------------------------------------------------------------


def report_person_step(
    *,
    pivot: TestRunTestCase,
    actor: PersonStepActor,
    person_step_id: int,
    outcome: str,
    note: str,
    output: str,
    expected_round: int | None = None,
) -> PersonStepActionResult:
    """The person performed the step and reports how it went; the agent confirms.

    ``expected_round``: the round the answer was given for (the Test Runner);
    a step reopened since (next round) refuses it. None skips the check.
    """
    report_outcome = _parse_outcome(outcome)
    note = _cap_note(note)
    if report_outcome == "different" and not note:
        raise PersonStepError(_NOTE_REQUIRED)
    output = _cap_output(output)
    with transaction.atomic():
        project, test_run = _lock_paused_run(pivot.test_run_id)
        locked_pivot, step = _lock_step(
            test_run, pivot.pk, person_step_id, _ACTIONABLE_STATUSES, expected_round
        )
        now = timezone.now()
        step.status = PersonStepStatus.REPORTED
        step.reported_at = now
        step.reported_by = actor.user
        step.reported_via = actor.via
        step.outcome = report_outcome
        step.note = note
        step.output = output
        step.save(
            update_fields=[
                "status",
                "reported_at",
                "reported_by",
                "reported_via",
                "outcome",
                "note",
                "output",
            ]
        )
        person = dataclasses.replace(
            _checkpoint_person(locked_pivot, step),
            state="reported",
            outcome=report_outcome,
            note=note,
            output=output,
            reporter=actor.label(),
            reported_at=now,
            continue_pending=True,
        )
        _resume_case(locked_pivot, person)
        return _after_action(project, test_run, locked_pivot, actor)


def overrule_person_step(
    *,
    pivot: TestRunTestCase,
    actor: PersonStepActor,
    person_step_id: int,
    note: str,
    expected_round: int | None = None,
) -> PersonStepActionResult:
    """The person overrules the agent's objection; the step is recorded passed."""
    note = _cap_note(note)
    with transaction.atomic():
        project, test_run = _lock_paused_run(pivot.test_run_id)
        locked_pivot, step = _lock_step(
            test_run, pivot.pk, person_step_id, _ACTIONABLE_STATUSES, expected_round
        )
        if step.status != PersonStepStatus.NOT_CONFIRMED:
            raise PersonStepError(_NOT_CONFIRMED_ONLY)
        # The agent records the pass on its next run; the worker closes the
        # row (``overruled``) then. ``closed_via`` marks the overrule (an
        # anonymous Test Runner answer has no ``closed_by``).
        step.status = PersonStepStatus.REPORTED
        step.closed_by = actor.user
        step.closed_via = actor.via
        if note:
            step.note = _cap_note(
                f"{step.note}\nOverruled: {note}" if step.note else note
            )
        step.save(update_fields=["status", "closed_by", "closed_via", "note"])
        base = _checkpoint_person(locked_pivot, step)
        person = dataclasses.replace(
            base,
            state="overruled",
            outcome=base.outcome or _stored_outcome(step),
            note=step.note,
            output=base.output or step.output,
            reporter=actor.label(),
            reported_at=step.reported_at or timezone.now(),
            continue_pending=True,
        )
        _resume_case(locked_pivot, person)
        return _after_action(project, test_run, locked_pivot, actor)


def mark_failed_person_step(
    *,
    pivot: TestRunTestCase,
    actor: PersonStepActor,
    person_step_id: int,
    note: str,
    output: str = "",
    expected_round: int | None = None,
) -> PersonStepActionResult:
    """The person judges the case Failed; the rest of the run continues.

    The agent records evidence first (``_mark_person_step``).
    """
    return _mark_person_step(
        pivot=pivot,
        actor=actor,
        person_step_id=person_step_id,
        note=note,
        output=output,
        expected_round=expected_round,
        verdict="failed",
    )


def mark_blocked_person_step(
    *,
    pivot: TestRunTestCase,
    actor: PersonStepActor,
    person_step_id: int,
    note: str,
    output: str = "",
    expected_round: int | None = None,
) -> PersonStepActionResult:
    """The person marks the case Blocked (``help_skipped``); the run continues.

    A person-only Blocked writer next to ``mark_blocked_after_help`` and the
    result-change Apply (``person_mark_verdict.write_person_mark_verdict``).
    The agent records evidence first (``_mark_person_step``).
    """
    return _mark_person_step(
        pivot=pivot,
        actor=actor,
        person_step_id=person_step_id,
        note=note,
        output=output,
        expected_round=expected_round,
        verdict="blocked",
    )


def _mark_person_step(
    *,
    pivot: TestRunTestCase,
    actor: PersonStepActor,
    person_step_id: int,
    note: str,
    output: str,
    expected_round: int | None,
    verdict: PersonMarkVerdict,
) -> PersonStepActionResult:
    """Mark failed / Mark blocked: the verdict is the person's, final.

    The row stays ``reported`` with the mark's close reason, the checkpoint
    ``person`` key becomes ``marked_failed`` / ``marked_blocked`` (note, output,
    who, when) and the case is queued first: the worker records the agent's
    evidence, then writes the verdict (``person_mark_verdict.apply_person_mark``)
    with the verdict text written into ``result`` here. When the run cannot
    continue (a gate refuses) the verdict is written now, without evidence.
    """
    note = _cap_note(note)
    output = _cap_output(output)
    with transaction.atomic():
        project, test_run = _lock_paused_run(pivot.test_run_id)
        locked_pivot, step = _lock_step(
            test_run, pivot.pk, person_step_id, _ACTIONABLE_STATUSES, expected_round
        )
        now = timezone.now()
        objection = _open_objection(step)
        result_text = _verdict_text(
            f"Marked {verdict} by {actor.label()} at step {step.step_num} "
            "while it was waiting for a person.",
            step,
            objection,
            note,
        )
        base = _checkpoint_person(locked_pivot, step)
        marked = with_person_step(
            locked_pivot.execution_checkpoint,
            dataclasses.replace(
                base,
                state="marked_failed" if verdict == "failed" else "marked_blocked",
                note=note,
                output=output or base.output,
                reporter=actor.label(),
                reported_at=now,
                continue_pending=True,
            ),
        )
        step.status = PersonStepStatus.REPORTED
        step.close_reason = (
            PersonStepClose.MARKED_FAILED
            if verdict == "failed"
            else PersonStepClose.MARKED_BLOCKED
        )
        step.closed_by = actor.user
        step.closed_via = actor.via
        if output:
            step.output = output
        step.save(
            update_fields=[
                "status",
                "close_reason",
                "closed_by",
                "closed_via",
                "output",
            ]
        )
        if not marked:
            # No saved progress to record evidence against.
            _write_mark_now(locked_pivot, step, verdict, result_text, now)
            log_no_evidence(locked_pivot.pk, "the case has no saved progress")
            return dataclasses.replace(
                _after_action(project, test_run, locked_pivot, actor),
                no_evidence="The case has no saved progress.",
            )
        checkpoint_before = locked_pivot.execution_checkpoint
        locked_pivot.execution_checkpoint = marked
        locked_pivot.status = TestRunTestCaseStatus.CREATED
        locked_pivot.blocked_reason = ""
        locked_pivot.result = result_text
        locked_pivot.finished_at = None
        locked_pivot.save(
            update_fields=[
                "execution_checkpoint",
                "status",
                "blocked_reason",
                "result",
                "finished_at",
                "updated_at",
            ]
        )
        result = _after_action(project, test_run, locked_pivot, actor)
        if result.queued:
            return result
        # The run cannot continue: the person's verdict stands now.
        locked_pivot.execution_checkpoint = without_person_step(checkpoint_before)
        _write_mark_now(locked_pivot, step, verdict, result_text, now)
        log_no_evidence(locked_pivot.pk, result.blocker)
        # Queued again: with no open case left the run finishes (no gates).
        blocker = _try_queue(test_run)
        return PersonStepActionResult(
            queued=not blocker, blocker=blocker, no_evidence=result.blocker
        )


def _write_mark_now(
    pivot: TestRunTestCase,
    step: CasePersonStep,
    verdict: PersonMarkVerdict,
    result_text: str,
    now: datetime,
) -> None:
    """The person's verdict without an evidence pass (locked case and row)."""
    pivot.result = result_text
    fields = write_person_mark_verdict(pivot, verdict, now)
    pivot.save(update_fields=[*fields, "execution_checkpoint", "result"])
    close_marked_row(pivot.pk, step.pk, verdict, now)


def mark_retest_person_step(
    *, pivot: TestRunTestCase, actor: PersonStepActor, note: str
) -> PersonStepActionResult:
    """The person marks the waiting case Needs re-test; the run continues.

    Web only (``actor.user`` is required). Marking is decisive, so the case's
    actionable open step is found here, with no stale-card or round check;
    it is closed (``cancelled``, ``marked_retest``) and the mark is written
    (``apply_retest_mark``): the result, log, screenshots and checkpoint are
    kept.
    """
    user = actor.user
    if user is None:
        raise PersonStepError(_SIGNED_IN_ONLY)
    retest_note = note.strip()[:RETEST_NOTE_MAX_LENGTH]
    with transaction.atomic():
        project, test_run = _lock_paused_run(pivot.test_run_id)
        person_step_id = (
            CasePersonStep.objects.filter(
                test_run_test_case_id=pivot.pk, status__in=_ACTIONABLE_STATUSES
            )
            .order_by("-id")
            .values_list("id", flat=True)
            .first()
        )
        if person_step_id is None:
            raise PersonStepError(_STEP_GONE)
        locked_pivot, step = _lock_step(
            test_run, pivot.pk, person_step_id, _ACTIONABLE_STATUSES, None
        )
        now = timezone.now()
        # Not performed: the case is re-run later.
        _close_step(
            step,
            PersonStepStatus.CANCELLED,
            PersonStepClose.MARKED_RETEST,
            actor,
            now,
        )
        # Verdict-discarding: recorded before the status write.
        record_case_activity(
            user=user, pivot_ids=[locked_pivot.pk], activity=CaseActivity.RETEST
        )
        apply_retest_mark(locked_pivot, user=user, note=retest_note, now=now)
        locked_pivot.save(update_fields=list(RETEST_MARK_FIELDS))
        return _after_action(project, test_run, locked_pivot, actor, activity=None)


def hand_back_person_step(test_run: TestRun) -> PersonStepActionResult | None:
    """The run is not in Human in the loop: the agent runs the open step itself.

    Called by the mode switch inside its transaction, with the project and
    run locked (the person step, then the case, are locked here). Only a
    ``waiting`` or ``not_confirmed`` step is handed back (from the current
    screen, no cleanup, no restore); a ``reported`` one finishes first.
    None when no step was handed back. A gate refusal keeps the hand-back and
    leaves the run paused ("Continue run"): ``queued`` False with the reason.
    """
    if test_run.status != TestRunStatus.PAUSED:
        return None
    step = (
        CasePersonStep.objects.select_for_update(of=("self",))
        .filter(
            test_run_test_case__test_run_id=test_run.pk,
            status__in=_ACTIONABLE_STATUSES,
        )
        .first()
    )
    if step is None:
        return None
    pivot = (
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(pk=step.test_run_test_case_id, test_run_id=test_run.pk)
        .first()
    )
    if pivot is None or pivot.status != TestRunTestCaseStatus.AWAITING_PERSON:
        return None
    step.status = PersonStepStatus.HANDED_BACK
    step.closed_at = timezone.now()
    step.save(update_fields=["status", "closed_at"])
    person = dataclasses.replace(
        _checkpoint_person(pivot, step), state="handed_back", continue_pending=True
    )
    _resume_case(pivot, person)
    project_id = test_run.project_id
    transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    transaction.on_commit(lambda: broadcast_project_attention_changed(project_id))
    blocker = _try_queue(test_run)
    return PersonStepActionResult(queued=not blocker, blocker=blocker)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lock_paused_run(test_run_id: int) -> tuple[Project, TestRun]:
    project, test_run = lock_project_and_run(test_run_id)
    if test_run.status != TestRunStatus.PAUSED:
        raise PersonStepError(_NOT_PAUSED)
    return project, test_run


def _lock_step(
    test_run: TestRun,
    pivot_id: int,
    person_step_id: int,
    statuses: tuple[str, ...],
    expected_round: int | None,
) -> tuple[TestRunTestCase, CasePersonStep]:
    """Lock the person step, then the case (after project and run).

    Raises ``PersonStepError`` unless the step is the case's, in one of
    ``statuses``, in ``expected_round`` when given, and the case waits for a
    person.
    """
    step = (
        CasePersonStep.objects.select_for_update()
        .filter(pk=person_step_id, test_run_test_case_id=pivot_id)
        .first()
    )
    if step is None or step.status not in statuses:
        raise PersonStepError(_STEP_GONE)
    if expected_round is not None and step.round != expected_round:
        raise PersonStepError(_STEP_GONE)
    pivot = (
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(pk=pivot_id, test_run_id=test_run.pk)
        .first()
    )
    if pivot is None or pivot.status != TestRunTestCaseStatus.AWAITING_PERSON:
        raise PersonStepError(_STEP_GONE)
    return pivot, step


def _checkpoint_person(
    pivot: TestRunTestCase, step: CasePersonStep
) -> CheckpointPersonStep:
    """The case's checkpoint ``person`` key for ``step``, or one built from the row.

    The orchestrator wrote the hand-off; the row is the fallback when the key
    is missing or belongs to another step.
    """
    stored = read_checkpoint_person_step(pivot.execution_checkpoint)
    if (
        stored is not None
        and stored.hand_off.step_num == step.step_num
        and stored.request_id in (None, step.pk)
    ):
        return dataclasses.replace(stored, request_id=step.pk)
    return CheckpointPersonStep(
        hand_off=PersonStepHandOff(
            step_num=step.step_num,
            kind=stored_hand_off_kind(step.kind),
            instruction=step.instruction,
            expected_result=step.expected_result,
            command=step.command,
            objection=step.agent_objection,
            how_to=stored_how_to(step.how_to),
            case_steps=tuple(item.num for item in stored_case_steps(step.case_steps)),
        ),
        request_id=step.pk,
        state="waiting",
        outcome=_stored_outcome(step),
        note=step.note,
        output=step.output,
        reporter=_row_reporter(step),
        requested_at=step.requested_at,
        reported_at=step.reported_at,
    )


def _resume_case(pivot: TestRunTestCase, person: CheckpointPersonStep) -> None:
    """Write the ``person`` key and put the case back to Created (saved once)."""
    pivot.execution_checkpoint = with_person_step(pivot.execution_checkpoint, person)
    pivot.status = TestRunTestCaseStatus.CREATED
    pivot.blocked_reason = ""
    pivot.result = ""
    pivot.finished_at = None
    pivot.save(
        update_fields=[
            "execution_checkpoint",
            "status",
            "blocked_reason",
            "result",
            "finished_at",
            "updated_at",
        ]
    )


def _close_step(
    step: CasePersonStep,
    status: PersonStepStatus,
    close_reason: PersonStepClose,
    actor: PersonStepActor,
    now: datetime,
) -> None:
    step.status = status
    step.close_reason = close_reason
    step.closed_by = actor.user
    step.closed_via = actor.via
    step.closed_at = now
    step.save(
        update_fields=["status", "close_reason", "closed_by", "closed_via", "closed_at"]
    )


def _row_reporter(step: CasePersonStep) -> str:
    """Who reported ``step`` as the checkpoint names them; "" before a report.

    An anonymous Test Runner report has no ``reported_by`` but a ``reported_via``.
    """
    if step.reported_by_id is None and step.reported_via != PersonStepChannel.RUNNER:
        return ""
    return PersonStepActor.from_row(step.reported_by, step.reported_via).label()


def _runner_shows_steps(project: Project) -> bool:
    """The project's Test Runner is connected and shows person steps (no query)."""
    capabilities = project.agent_capabilities
    return (
        project.agent_connected
        and isinstance(capabilities, list)
        and ClientCapability.PERSON_STEPS_V1.value in capabilities
    )


def _after_action(
    project: Project,
    test_run: TestRun,
    pivot: TestRunTestCase,
    actor: PersonStepActor,
    *,
    activity: CaseActivity | None = CaseActivity.PERSON_STEP,
) -> PersonStepActionResult:
    """Record ``activity`` (None: the caller recorded its own), broadcast after
    commit, then queue the pending cases (savepoint)."""
    # An anonymous Test Runner answer (no user) is not attributed.
    if activity is not None:
        record_case_activity(user=actor.user, pivot_ids=[pivot.pk], activity=activity)
    # The person performed the step; credit_cases skips an anonymous answer.
    if activity == CaseActivity.PERSON_STEP:
        credit_cases(user=actor.user, pivot_ids=[pivot.pk])
    project_id = project.pk
    # Registered before the queue's own callbacks, so a broker failure that
    # cancels the run is the last word on the page.
    transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    # The open step closed or moved on: the members' attention lists change.
    transaction.on_commit(lambda: broadcast_project_attention_changed(project_id))
    blocker = _try_queue(test_run)
    if blocker:
        return PersonStepActionResult(queued=False, blocker=blocker)
    return PersonStepActionResult(queued=True)


def _try_queue(test_run: TestRun) -> str:
    """Queue the pending cases; the refusal's message, or "" when queued.

    A refusal rolls back only the queueing (a savepoint), so the caller's
    change is kept and the run stays paused.
    """
    try:
        with transaction.atomic():
            queue_pending_cases(test_run)
    except RunHelpError as exc:
        test_run.refresh_from_db(
            fields=["status", "execution_generation", "finished_at"]
        )
        return exc.message
    return ""


def _step_label(step_num: int, checkpoint_json: object) -> str:
    total = _plan_size(checkpoint_json)
    if total >= step_num:
        return f"Step {step_num} of {total}"
    return f"Step {step_num}"


def _plan_size(checkpoint_json: object) -> int:
    if not isinstance(checkpoint_json, dict):
        return 0
    plan = checkpoint_json.get("plan")
    return len(plan) if isinstance(plan, list) else 0


def _parse_outcome(outcome: str) -> PersonReportOutcome:
    if outcome == PersonStepOutcome.AS_EXPECTED:
        return "as_expected"
    if outcome == PersonStepOutcome.DIFFERENT:
        return "different"
    raise PersonStepError(_UNKNOWN_OUTCOME)


def _stored_outcome(step: CasePersonStep) -> PersonReportOutcome | None:
    return _parse_outcome(step.outcome) if step.outcome else None


def _open_objection(step: CasePersonStep) -> str:
    """The agent's objection while the step is ``not_confirmed``; "" otherwise."""
    if step.status == PersonStepStatus.NOT_CONFIRMED:
        return step.agent_objection
    return ""


def _marked_verdict(step: CasePersonStep) -> str:
    if step.status != PersonStepStatus.REPORTED:
        return ""
    if step.close_reason == PersonStepClose.MARKED_FAILED:
        return "failed"
    if step.close_reason == PersonStepClose.MARKED_BLOCKED:
        return "blocked"
    return ""


def _cap_note(note: str) -> str:
    return note.strip()[:HELP_NOTE_MAX_LENGTH]


def _cap_output(output: str) -> str:
    """Keep the head and tail of a too-long pasted output."""
    if len(output) <= PERSON_STEP_OUTPUT_MAX_LENGTH:
        return output
    room = PERSON_STEP_OUTPUT_MAX_LENGTH - len(_OUTPUT_OMITTED)
    head = room // 2
    return output[:head] + _OUTPUT_OMITTED + output[-(room - head) :]


def _verdict_text(
    headline: str, step: CasePersonStep, objection: str, note: str
) -> str:
    parts = [headline, f"The step: {step.instruction}"]
    if objection:
        parts.append(f"The agent could not confirm: {objection}")
    if note:
        parts.append(f"Note: {note}")
    return "\n".join(parts)
