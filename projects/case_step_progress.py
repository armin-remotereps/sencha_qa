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
    StepDecision,
    SubTaskAttempt,
)
from projects.models import TestRunTestCase, TestRunTestCaseStatus

# Caps on LLM text sent to the browser; the page has the full log for more.
PAYLOAD_TEXT_LIMIT = 1000


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    NOT_RUN = "not_run"


STEP_STATUS_LABELS: dict[StepStatus, str] = {
    StepStatus.PENDING: "Pending",
    StepStatus.RUNNING: "Running",
    StepStatus.PASSED: "Passed",
    StepStatus.FAILED: "Failed",
    StepStatus.INTERRUPTED: "Interrupted",
    StepStatus.NOT_RUN: "Not run",
}


class StepActivity(StrEnum):
    NONE = ""
    STARTING = "starting"
    RUNNING = "running"
    EVALUATING = "evaluating"
    RECOVERING = "recovering"
    RESTORING = "restoring"
    VERDICT = "verdict"


_ACTIVITY_LABELS: dict[StepActivity, str] = {
    StepActivity.NONE: "",
    StepActivity.STARTING: "Starting",
    StepActivity.RUNNING: "Running the step",
    StepActivity.EVALUATING: "Checking the failure",
    StepActivity.RECOVERING: "Recovering",
    StepActivity.RESTORING: "Restoring state before resuming",
    StepActivity.VERDICT: "Writing the verdict",
}

_PHASE_ACTIVITIES: dict[OrchestratorPhase, StepActivity] = {
    "step": StepActivity.RUNNING,
    "evaluating": StepActivity.EVALUATING,
    "recovery": StepActivity.RECOVERING,
    "resume": StepActivity.RESTORING,
    "verdict": StepActivity.VERDICT,
}


@dataclass(frozen=True)
class StepAttemptRow:
    label: str
    passed: bool
    summary: str
    error: str
    duration_seconds: int | None


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

    @property
    def status_label(self) -> str:
        return STEP_STATUS_LABELS[self.status]


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


def build_case_step_progress(
    checkpoint_data: object,
    case_status: str,
    case_started_at: datetime | None,
) -> CaseStepProgress | None:
    """Rows for every planned step; ``None`` without a usable checkpoint."""
    checkpoint = checkpoint_from_json(checkpoint_data)
    if checkpoint is None:
        return None
    running = case_status == TestRunTestCaseStatus.IN_PROGRESS
    open_case = running or case_status == TestRunTestCaseStatus.CREATED
    interrupted = case_status in (
        TestRunTestCaseStatus.BLOCKED,
        TestRunTestCaseStatus.CANCELLED,
    )
    cursor = checkpoint.next_step
    activity = _current_activity(checkpoint, case_started_at) if running else None

    final_index = {
        attempt.step_num: index
        for index, attempt in enumerate(checkpoint.attempts)
        if attempt.kind == "step"
    }
    rows: list[CaseStepRow] = []
    for num, task in enumerate(checkpoint.plan, 1):
        own = [a for a in checkpoint.attempts if a.step_num == num]
        final = checkpoint.attempts[final_index[num]] if num in final_index else None
        status, row_activity = _row_status(
            num=num,
            final=final,
            cursor=cursor,
            open_case=open_case,
            interrupted=interrupted,
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
                history=_history(own),
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
    )


def _current_activity(
    checkpoint: OrchestratorCheckpoint, case_started_at: datetime | None
) -> StepActivity:
    # A resumed case keeps the phase it was interrupted in until its worker
    # writes the first checkpoint of the new execution.
    if (
        checkpoint.phase_started_at is not None
        and case_started_at is not None
        and checkpoint.phase_started_at < case_started_at
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
    activity: StepActivity | None,
) -> tuple[StepStatus, StepActivity]:
    if final is not None and final.result.status == "pass":
        return StepStatus.PASSED, StepActivity.NONE
    if num == cursor and activity is not None:
        return StepStatus.RUNNING, activity
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


def _history(attempts: Sequence[SubTaskAttempt]) -> tuple[StepAttemptRow, ...]:
    """Every attempt of a step; empty when its single attempt says it all."""
    if len(attempts) <= 1:
        return ()
    rows: list[StepAttemptRow] = []
    step_count = 0
    for attempt in attempts:
        if attempt.kind == "step":
            step_count += 1
            label = f"Attempt {step_count}"
        elif attempt.kind == "recovery":
            label = "Recovery"
        elif attempt.result.status == "pass":
            label = "State restored before this step"
        else:
            label = "Could not restore state before this step"
        rows.append(
            StepAttemptRow(
                label=label,
                passed=attempt.result.status == "pass",
                summary=attempt.result.summary,
                error=attempt.result.error or "",
                duration_seconds=attempt.duration_seconds,
            )
        )
    return tuple(rows)


class StepAttemptPayload(TypedDict):
    label: str
    passed: bool
    summary: str
    error: str
    duration_seconds: int | None


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
    case_started_at: datetime | None,
) -> StepProgressPayload:
    """Payload for a case straight from its stored checkpoint and status."""
    progress = build_case_step_progress(checkpoint_data, case_status, case_started_at)
    return step_progress_payload(
        progress,
        planning=progress is None and case_status == TestRunTestCaseStatus.IN_PROGRESS,
    )


def pivot_step_payload(pivot: TestRunTestCase) -> StepProgressPayload:
    """The case's step rows from its loaded checkpoint and status (no query)."""
    return case_step_payload(pivot.execution_checkpoint, pivot.status, pivot.started_at)


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
            )
            for item in row.history
        ],
    )


def _cap(text: str) -> str:
    if len(text) <= PAYLOAD_TEXT_LIMIT:
        return text
    return text[: PAYLOAD_TEXT_LIMIT - 1].rstrip() + "…"
