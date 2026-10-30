"""Turn a run case's orchestrator checkpoint into step rows for display.

Pure: no ORM and no import from ``agents.services`` (that package imports
``projects.services``). The case page, its WebSocket events and the TestRail
result comments all read step status from here, so they never disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypedDict

from agents.checkpoint import checkpoint_from_json
from agents.types import (
    OrchestratorCheckpoint,
    OrchestratorPhase,
    PlanRevisionRecord,
    StepDecision,
    SubTaskAttempt,
)
from projects.case_step_labels import format_case_steps_label
from projects.models import TestRunTestCase, TestRunTestCaseStatus

# A Needs re-test case marked from one of these verdicts shows its steps as
# they were; marked from anything else, its cursor step reads as interrupted.
_RETEST_KEPT_VERDICTS: tuple[str, ...] = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
)

# Caps on LLM text sent to the browser; the page has the full log for more.
PAYLOAD_TEXT_LIMIT = 1000


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    NOT_RUN = "not_run"
    # The case is Need help at this step: it re-runs once the machine is fixed.
    AWAITING_HELP = "awaiting_help"
    # Human in the loop: the step waits for a person to perform it.
    AWAITING_PERSON = "awaiting_person"


STEP_STATUS_LABELS: dict[StepStatus, str] = {
    StepStatus.PENDING: "Pending",
    StepStatus.RUNNING: "Running",
    StepStatus.PASSED: "Passed",
    StepStatus.FAILED: "Failed",
    StepStatus.INTERRUPTED: "Interrupted",
    StepStatus.NOT_RUN: "Not run",
    StepStatus.AWAITING_HELP: "Needs help",
    StepStatus.AWAITING_PERSON: "Your turn",
}


class StepActivity(StrEnum):
    NONE = ""
    STARTING = "starting"
    RUNNING = "running"
    EVALUATING = "evaluating"
    RECOVERING = "recovering"
    RESTORING = "restoring"
    VERDICT = "verdict"
    AWAITING_HELP = "awaiting_help"
    AWAITING_PERSON = "awaiting_person"


_ACTIVITY_LABELS: dict[StepActivity, str] = {
    StepActivity.NONE: "",
    StepActivity.STARTING: "Starting",
    StepActivity.RUNNING: "Running the step",
    StepActivity.EVALUATING: "Checking the failure",
    StepActivity.RECOVERING: "Recovering",
    StepActivity.RESTORING: "Restoring state before resuming",
    StepActivity.VERDICT: "Writing the verdict",
    StepActivity.AWAITING_HELP: "Waiting for help",
    StepActivity.AWAITING_PERSON: "Waiting for a person",
}

_PHASE_ACTIVITIES: dict[OrchestratorPhase, StepActivity] = {
    "step": StepActivity.RUNNING,
    "evaluating": StepActivity.EVALUATING,
    "recovery": StepActivity.RECOVERING,
    "resume": StepActivity.RESTORING,
    "verdict": StepActivity.VERDICT,
    "awaiting_help": StepActivity.AWAITING_HELP,
    "awaiting_person": StepActivity.AWAITING_PERSON,
}

# Human in the loop: who performed a step (attempt label and row note).
PERFORMED_BY_PERSON_LABEL = "Performed by a person"


@dataclass(frozen=True)
class StepAttemptRow:
    label: str
    passed: bool
    summary: str
    error: str
    duration_seconds: int | None
    # "agent", or "person" when a person performed it (Human in the loop).
    performed_by: str = "agent"


@dataclass(frozen=True)
class CaseStepRow:
    num: int
    description: str
    expected_result: str
    status: StepStatus
    activity: StepActivity
    summary: str
    error: str
    attempt_count: int
    recovery_count: int
    duration_seconds: int | None
    decision: StepDecision | None
    history: tuple[StepAttemptRow, ...]
    # Earlier plan's step this one was carried from by a help-chat redesign
    # (counted as passed, not re-run), and its label; None / "" otherwise.
    carried_from: int | None = None
    carried_label: str = ""
    # Who performed the step's final attempt: "agent", or "person" (Human in
    # the loop, confirmed or overruled; the summary says which).
    performed_by: str = "agent"
    # The test case's own numbered steps this plan step covers, e.g.
    # "TestRail steps 3–5"; "" when the planner tagged none.
    case_steps_label: str = ""

    @property
    def status_label(self) -> str:
        return STEP_STATUS_LABELS[self.status]

    @property
    def performed_by_label(self) -> str:
        """ "Performed by a person", or "" for the agent."""
        return PERFORMED_BY_PERSON_LABEL if self.performed_by == "person" else ""

    @property
    def status_text(self) -> str:
        """The status label plus who performed the step, for plain-text reports."""
        if self.performed_by == "person":
            return f"{self.status_label} ({PERFORMED_BY_PERSON_LABEL.lower()})"
        return self.status_label


@dataclass(frozen=True)
class CaseStepProgress:
    rows: tuple[CaseStepRow, ...]
    # Step the orchestrator is on while the case runs; None otherwise.
    current_step: int | None
    activity: StepActivity
    phase_started_at: datetime | None
    passed: int
    failed: int
    not_run: int
    total: int
    resumed: bool
    # Plans replaced by help-chat or result-chat redesigns, oldest first (the checkpoint keeps
    # the newest few).
    earlier_plans: tuple[EarlierPlan, ...] = ()


@dataclass(frozen=True)
class EarlierPlanStep:
    num: int
    description: str
    status_label: str
    # New step it was carried to; None when it was dropped.
    carried_to: int | None


@dataclass(frozen=True)
class EarlierPlan:
    """A plan a help chat or a result chat redesigned, with how its steps ended."""

    # ``plan_version`` the redesign gave the checkpoint.
    version: int
    reason: str
    # None for a result-chat (review) redesign.
    help_request_id: int | None
    # Step of that plan the case was waiting for help at when it was
    # redesigned; for a review redesign, its last failed step (0 when none).
    help_step_num: int
    resume_step: int
    steps: tuple[EarlierPlanStep, ...]
    # ``help``: redesigned in a help chat; ``review``: after a result review.
    source: str = "help"

    @property
    def is_review(self) -> bool:
        return self.source == "review"

    @property
    def label(self) -> str:
        """ "help at step N" or "after result review" (plain text)."""
        if self.is_review:
            return "after result review"
        return f"help at step {self.help_step_num}"


def build_case_step_progress(
    checkpoint_data: object,
    case_status: str,
    segment_started_at: datetime | None,
    *,
    retest_from_status: str = "",
) -> CaseStepProgress | None:
    """Rows for every planned step; ``None`` without a usable checkpoint.

    ``retest_from_status`` is the status a Needs re-test case was marked from
    (`retest_from_status_of`): one marked from a Passed or Failed verdict shows
    its steps as they were, one marked mid-run (or from Blocked) shows the step
    it stopped at as interrupted.
    """
    checkpoint = checkpoint_from_json(checkpoint_data)
    if checkpoint is None:
        return None
    running = case_status == TestRunTestCaseStatus.IN_PROGRESS
    awaiting_help = case_status == TestRunTestCaseStatus.NEED_HELP
    awaiting_person = case_status == TestRunTestCaseStatus.AWAITING_PERSON
    # A Need help (or person step) case continues later: its later steps are
    # still pending.
    open_case = (
        running
        or awaiting_help
        or awaiting_person
        or case_status == TestRunTestCaseStatus.CREATED
    )
    interrupted = case_status in (
        TestRunTestCaseStatus.BLOCKED,
        TestRunTestCaseStatus.CANCELLED,
    ) or (
        case_status == TestRunTestCaseStatus.NEEDS_RETEST
        and retest_from_status not in _RETEST_KEPT_VERDICTS
    )
    cursor = checkpoint.next_step
    activity = _current_activity(checkpoint, segment_started_at) if running else None

    final_index = {
        attempt.step_num: index
        for index, attempt in enumerate(checkpoint.attempts)
        if attempt.kind == "step"
    }
    revisions = checkpoint.plan_revisions
    rows: list[CaseStepRow] = []
    for num, task in enumerate(checkpoint.plan, 1):
        own = [a for a in checkpoint.attempts if a.step_num == num]
        final = checkpoint.attempts[final_index[num]] if num in final_index else None
        carried_from = final.carried_from if final is not None else None
        status, row_activity = _row_status(
            num=num,
            final=final,
            cursor=cursor,
            open_case=open_case,
            interrupted=interrupted,
            awaiting_help=awaiting_help,
            awaiting_person=awaiting_person,
            activity=activity,
        )
        decision = next(
            (d for d in reversed(checkpoint.decisions) if d.step_num == num), None
        )
        rows.append(
            CaseStepRow(
                num=num,
                description=task.description,
                expected_result=task.expected_result,
                status=status,
                activity=row_activity,
                summary=final.result.summary if final else "",
                error=(final.result.error or "") if final else "",
                attempt_count=sum(1 for a in own if a.kind == "step"),
                recovery_count=sum(1 for a in own if a.kind == "recovery"),
                duration_seconds=_total_duration(own),
                decision=(
                    decision
                    if final is not None and final.result.status == "fail"
                    else None
                ),
                history=_history(
                    own,
                    earlier=_earlier_attempts(revisions, carried_from),
                ),
                carried_from=carried_from,
                carried_label=_carried_label(revisions, carried_from),
                performed_by=final.performed_by if final is not None else "agent",
                case_steps_label=format_case_steps_label(
                    task.case_steps, testrail=checkpoint.case_steps_testrail
                ),
            )
        )

    in_plan = 1 <= cursor <= len(checkpoint.plan)
    return CaseStepProgress(
        rows=tuple(rows),
        current_step=cursor if running and in_plan else None,
        activity=activity or StepActivity.NONE,
        phase_started_at=checkpoint.phase_started_at if running else None,
        passed=sum(1 for r in rows if r.status == StepStatus.PASSED),
        failed=sum(1 for r in rows if r.status == StepStatus.FAILED),
        not_run=sum(
            1 for r in rows if r.status in (StepStatus.NOT_RUN, StepStatus.INTERRUPTED)
        ),
        total=len(rows),
        resumed=any(a.kind == "resume" for a in checkpoint.attempts),
        earlier_plans=tuple(_earlier_plan(record) for record in revisions),
    )


def _carried_label(
    revisions: Sequence[PlanRevisionRecord], carried_from: int | None
) -> str:
    """ "Passed under the earlier plan as step k: <description>"; "" if not carried.

    Same wording as ``agents.plan_revision.attempt_origin_label``, which this
    module cannot import: that module pulls in ``agents.services``, which
    imports ``projects.services``, which imports this module.
    """
    if carried_from is None:
        return ""
    label = f"Passed under the earlier plan as step {carried_from}"
    # Carried attempts always come from the newest revision.
    if revisions:
        previous_plan = revisions[-1].previous_plan
        if 1 <= carried_from <= len(previous_plan):
            return f"{label}: {previous_plan[carried_from - 1][0]}"
    return label


def _earlier_attempts(
    revisions: Sequence[PlanRevisionRecord], carried_from: int | None
) -> list[SubTaskAttempt]:
    """The newest earlier plan's attempts of the step a row was carried from."""
    if carried_from is None or not revisions:
        return []
    return [a for a in revisions[-1].earlier_attempts if a.step_num == carried_from]


def _earlier_plan(record: PlanRevisionRecord) -> EarlierPlan:
    finals: dict[int, SubTaskAttempt] = {}
    for attempt in record.earlier_attempts:
        if attempt.kind == "step":
            finals[attempt.step_num] = attempt
    carried_to = {old_step: new_step for new_step, old_step in record.mapping}
    steps: list[EarlierPlanStep] = []
    for num, (description, _expected) in enumerate(record.previous_plan, 1):
        final = finals.get(num)
        if final is not None and final.result.status == "pass":
            status = StepStatus.PASSED
        elif num == record.paused_step and record.source == "help":
            # The case was waiting for help here when the plan was redesigned.
            status = StepStatus.AWAITING_HELP
        elif final is not None:
            status = StepStatus.FAILED
        else:
            status = StepStatus.NOT_RUN
        steps.append(
            EarlierPlanStep(
                num=num,
                description=description,
                status_label=STEP_STATUS_LABELS[status],
                carried_to=carried_to.get(num),
            )
        )
    return EarlierPlan(
        version=record.version,
        reason=record.reason,
        help_request_id=record.help_request_id,
        help_step_num=record.paused_step,
        resume_step=record.resume_step,
        steps=tuple(steps),
        source=record.source,
    )


def _current_activity(
    checkpoint: OrchestratorCheckpoint, segment_started_at: datetime | None
) -> StepActivity:
    # A resumed case keeps the phase it was interrupted in until its worker
    # writes the first checkpoint of the new execution (``segment_started_at``
    # is when the current worker picked the case up).
    if (
        checkpoint.phase_started_at is not None
        and segment_started_at is not None
        and checkpoint.phase_started_at < segment_started_at
    ):
        return StepActivity.STARTING
    return _PHASE_ACTIVITIES[checkpoint.phase]


def _row_status(
    *,
    num: int,
    final: SubTaskAttempt | None,
    cursor: int,
    open_case: bool,
    interrupted: bool,
    awaiting_help: bool,
    awaiting_person: bool,
    activity: StepActivity | None,
) -> tuple[StepStatus, StepActivity]:
    if final is not None and final.result.status == "pass":
        return StepStatus.PASSED, StepActivity.NONE
    if num == cursor and activity is not None:
        return StepStatus.RUNNING, activity
    if num == cursor and awaiting_help:
        return StepStatus.AWAITING_HELP, StepActivity.AWAITING_HELP
    if num == cursor and awaiting_person:
        return StepStatus.AWAITING_PERSON, StepActivity.AWAITING_PERSON
    if num == cursor and interrupted:
        return StepStatus.INTERRUPTED, StepActivity.NONE
    if final is not None:
        return StepStatus.FAILED, StepActivity.NONE
    if open_case:
        return StepStatus.PENDING, StepActivity.NONE
    return StepStatus.NOT_RUN, StepActivity.NONE


def _total_duration(attempts: Sequence[SubTaskAttempt]) -> int | None:
    durations = [a.duration_seconds for a in attempts if a.duration_seconds is not None]
    return sum(durations) if durations else None


def _history(
    attempts: Sequence[SubTaskAttempt],
    *,
    earlier: Sequence[SubTaskAttempt] = (),
) -> tuple[StepAttemptRow, ...]:
    """Every attempt of a step; empty when its single attempt says it all.

    ``earlier`` are a carried step's attempts under the earlier plan, listed
    first and labelled "Earlier plan · …".
    """
    if len(earlier) + len(attempts) <= 1:
        return ()
    return (
        *_attempt_rows(earlier, prefix="Earlier plan · "),
        *_attempt_rows(attempts, prefix=""),
    )


def _attempt_rows(
    attempts: Sequence[SubTaskAttempt], *, prefix: str
) -> list[StepAttemptRow]:
    rows: list[StepAttemptRow] = []
    step_count = 0
    for attempt in attempts:
        if attempt.carried_from is not None:
            label = "Carried over"
        elif attempt.kind == "step":
            step_count += 1
            label = f"Attempt {step_count}"
        elif attempt.kind == "recovery":
            label = "Recovery"
        elif attempt.result.status == "pass":
            label = "State restored before this step"
        else:
            label = "Could not restore state before this step"
        if attempt.performed_by == "person":
            label += f" · {PERFORMED_BY_PERSON_LABEL}"
        rows.append(
            StepAttemptRow(
                label=f"{prefix}{label}",
                passed=attempt.result.status == "pass",
                summary=attempt.result.summary,
                error=attempt.result.error or "",
                duration_seconds=attempt.duration_seconds,
                performed_by=attempt.performed_by,
            )
        )
    return rows


class StepAttemptPayload(TypedDict):
    label: str
    passed: bool
    summary: str
    error: str
    duration_seconds: int | None
    # "agent" or "person".
    performed_by: str


class StepDecisionPayload(TypedDict):
    action: str
    reason: str


class StepRowPayload(TypedDict):
    num: int
    description: str
    expected_result: str
    status: str
    status_label: str
    activity: str
    activity_label: str
    summary: str
    error: str
    attempt_count: int
    recovery_count: int
    duration_seconds: int | None
    decision: StepDecisionPayload | None
    history: list[StepAttemptPayload]
    carried_from: int | None
    carried_label: str
    # "agent" or "person"; the label is "" for the agent.
    performed_by: str
    performed_by_label: str
    # "TestRail steps 3–5" / "Case steps 1, 3"; "" when none.
    case_steps_label: str


class StepProgressPayload(TypedDict):
    """JSON-safe step progress for ``json_script`` and WebSocket events."""

    available: bool
    planning: bool
    rows: list[StepRowPayload]
    current_step: int | None
    activity_label: str
    phase_started_at: str | None
    passed: int
    failed: int
    not_run: int
    total: int
    resumed: bool


def step_progress_payload(
    progress: CaseStepProgress | None, *, planning: bool
) -> StepProgressPayload:
    """``planning`` is True while a running case has no checkpoint yet."""
    if progress is None:
        return StepProgressPayload(
            available=False,
            planning=planning,
            rows=[],
            current_step=None,
            activity_label="",
            phase_started_at=None,
            passed=0,
            failed=0,
            not_run=0,
            total=0,
            resumed=False,
        )
    return StepProgressPayload(
        available=True,
        planning=False,
        rows=[_row_payload(row) for row in progress.rows],
        current_step=progress.current_step,
        activity_label=_ACTIVITY_LABELS[progress.activity],
        phase_started_at=(
            progress.phase_started_at.isoformat() if progress.phase_started_at else None
        ),
        passed=progress.passed,
        failed=progress.failed,
        not_run=progress.not_run,
        total=progress.total,
        resumed=progress.resumed,
    )


def case_step_payload(
    checkpoint_data: object,
    case_status: str,
    segment_started_at: datetime | None,
    *,
    retest_from_status: str = "",
) -> StepProgressPayload:
    """Payload for a case straight from its stored checkpoint and status."""
    progress = build_case_step_progress(
        checkpoint_data,
        case_status,
        segment_started_at,
        retest_from_status=retest_from_status,
    )
    return step_progress_payload(progress, planning=_is_planning(progress, case_status))


def retest_from_status_of(pivot: TestRunTestCase) -> str:
    """The status a Needs re-test case was marked from; "" for any other case.

    Reads ``retest_from_status`` only for a Needs re-test case, so a pivot
    loaded without that column costs no extra query unless it is marked.
    """
    if pivot.status != TestRunTestCaseStatus.NEEDS_RETEST:
        return ""
    return pivot.retest_from_status


def pivot_step_payload(pivot: TestRunTestCase) -> StepProgressPayload:
    """The case's step rows from its loaded checkpoint and status (no query)."""
    return case_step_payload(
        pivot.execution_checkpoint,
        pivot.status,
        pivot.segment_started_at,
        retest_from_status=retest_from_status_of(pivot),
    )


def case_step_view(
    pivot: TestRunTestCase,
) -> tuple[StepProgressPayload, tuple[EarlierPlan, ...]]:
    """The case page's step rows and earlier plans from one checkpoint parse."""
    progress = build_case_step_progress(
        pivot.execution_checkpoint,
        pivot.status,
        pivot.segment_started_at,
        retest_from_status=retest_from_status_of(pivot),
    )
    payload = step_progress_payload(
        progress, planning=_is_planning(progress, pivot.status)
    )
    return payload, progress.earlier_plans if progress is not None else ()


def _is_planning(progress: CaseStepProgress | None, case_status: str) -> bool:
    """A running case without a checkpoint yet is still planning its steps."""
    return progress is None and case_status == TestRunTestCaseStatus.IN_PROGRESS


def _row_payload(row: CaseStepRow) -> StepRowPayload:
    return StepRowPayload(
        num=row.num,
        description=_cap(row.description),
        expected_result=_cap(row.expected_result),
        status=row.status.value,
        status_label=row.status_label,
        activity=row.activity.value,
        activity_label=_ACTIVITY_LABELS[row.activity],
        summary=_cap(row.summary),
        error=_cap(row.error),
        attempt_count=row.attempt_count,
        recovery_count=row.recovery_count,
        duration_seconds=row.duration_seconds,
        decision=(
            StepDecisionPayload(
                action=row.decision.action, reason=_cap(row.decision.reason)
            )
            if row.decision
            else None
        ),
        history=[
            StepAttemptPayload(
                label=item.label,
                passed=item.passed,
                summary=_cap(item.summary),
                error=_cap(item.error),
                duration_seconds=item.duration_seconds,
                performed_by=item.performed_by,
            )
            for item in row.history
        ],
        carried_from=row.carried_from,
        carried_label=_cap(row.carried_label),
        performed_by=row.performed_by,
        performed_by_label=row.performed_by_label,
        case_steps_label=row.case_steps_label,
    )


def _cap(text: str) -> str:
    if len(text) <= PAYLOAD_TEXT_LIMIT:
        return text
    return text[: PAYLOAD_TEXT_LIMIT - 1].rstrip() + "…"
