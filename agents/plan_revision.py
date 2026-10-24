"""Validate and apply a help-chat redesign of a case's test plan.

Pure: no ORM. A redesign is a full new step list whose leading steps may be
*carried* from earlier steps that passed; carried steps count as passed and the
case resumes at the first step that is not carried.

Rules (``validate_plan_revision``):

1. 1..max_steps steps, non-empty descriptions, capped text, bounded timeouts.
2. Only an earlier step whose final step attempt passed can be carried.
3. Each earlier step is carried at most once.
4. Carried steps form a prefix of the new plan (the checkpoint has one cursor:
   every consumer treats the steps before it as done).
5. Within that prefix the earlier step numbers strictly increase, so the carried
   state is in the order the machine went through it; gaps (dropped steps) are
   allowed.
6. At least one step is not carried: Continue must run something.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from agents.checkpoint import MAX_PLAN_REVISION_RECORDS, checkpoint_to_json
from agents.exceptions import PlanRevisionInvalid
from agents.services.sub_task_budget import TimeoutBounds, resolve_timeout_seconds
from agents.types import (
    OrchestratorCheckpoint,
    PlanRevisionRecord,
    SubTask,
    SubTaskAttempt,
    SubTaskResult,
)

MAX_DESCRIPTION_CHARS = 2000
MAX_EXPECTED_RESULT_CHARS = 1000
MAX_TOTAL_TEXT_CHARS = 24_000
MAX_REASON_CHARS = 1000
# Caps for the earlier plan kept in a revision record.
_RECORD_STEP_TEXT_CHARS = 500
_RECORD_PAUSED_DIGEST_CHARS = 6000

OldStepStatus = Literal["passed", "failed", "not_run", "needs_help"]
NewStepState = Literal["carried", "resume", "run"]

_OLD_STATUS_LABELS: dict[OldStepStatus, str] = {
    "passed": "Passed",
    "failed": "Failed",
    "not_run": "Not run",
    "needs_help": "Needs help",
}


@dataclass(frozen=True)
class PlanStepInput:
    """One proposed step as the agent sent it (timeout not yet resolved)."""

    description: str
    expected_result: str
    raw_timeout: object
    carried_from: int | None


@dataclass(frozen=True)
class RevisionStep:
    """One validated step of a redesigned plan."""

    description: str
    expected_result: str
    timeout_seconds: int
    carried_from: int | None


@dataclass(frozen=True)
class ValidatedRevision:
    steps: tuple[RevisionStep, ...]
    # First step that is not carried (carried count + 1).
    resume_step: int


@dataclass(frozen=True)
class PlanStepSnapshot:
    """An earlier step as it stood when the redesign was proposed."""

    description: str
    expected_result: str
    status: OldStepStatus


@dataclass(frozen=True)
class DraftOldRow:
    num: int
    description: str
    status: OldStepStatus
    status_label: str
    # New step it is carried to; None means dropped (or simply not carried).
    carried_to: int | None


@dataclass(frozen=True)
class DraftNewRow:
    num: int
    description: str
    expected_result: str
    carried_from: int | None
    state: NewStepState
    state_label: str


@dataclass(frozen=True)
class PlanDraftView:
    """Side-by-side view of a proposed redesign for the help panel."""

    revision_id: int
    reason: str
    resume_step: int
    carried_count: int
    old_rows: tuple[DraftOldRow, ...]
    new_rows: tuple[DraftNewRow, ...]


def plan_fingerprint(checkpoint: OrchestratorCheckpoint) -> str:
    """sha256 of the plan, attempts, cursor and plan version."""
    data = checkpoint_to_json(checkpoint)
    canonical = {
        "plan": data["plan"],
        "attempts": data["attempts"],
        "next_step": checkpoint.next_step,
        "plan_version": checkpoint.plan_version,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def carriable_steps(checkpoint: OrchestratorCheckpoint) -> dict[int, SubTaskAttempt]:
    """Earlier steps whose final step attempt passed, with that attempt."""
    finals: dict[int, SubTaskAttempt] = {}
    for attempt in checkpoint.attempts:
        if attempt.kind == "step":
            finals[attempt.step_num] = attempt
    return {
        step_num: attempt
        for step_num, attempt in sorted(finals.items())
        if attempt.result.status == "pass" and 1 <= step_num <= len(checkpoint.plan)
    }


def snapshot_plan(
    checkpoint: OrchestratorCheckpoint,
) -> tuple[PlanStepSnapshot, ...]:
    """Every step of the current plan with its status; the cursor needs help."""
    finals: dict[int, SubTaskResult] = {}
    for attempt in checkpoint.attempts:
        if attempt.kind == "step":
            finals[attempt.step_num] = attempt.result
    snapshots: list[PlanStepSnapshot] = []
    for num, task in enumerate(checkpoint.plan, start=1):
        status: OldStepStatus
        if num == checkpoint.next_step and checkpoint.phase == "awaiting_help":
            status = "needs_help"
        elif num not in finals:
            status = "not_run"
        elif finals[num].status == "pass":
            status = "passed"
        else:
            status = "failed"
        snapshots.append(
            PlanStepSnapshot(
                description=task.description,
                expected_result=task.expected_result,
                status=status,
            )
        )
    return tuple(snapshots)


def validate_plan_revision(
    checkpoint: OrchestratorCheckpoint,
    steps: Sequence[PlanStepInput],
    *,
    bounds: TimeoutBounds,
    max_steps: int,
) -> ValidatedRevision:
    """Check a proposed redesign; raises ``PlanRevisionInvalid`` (model-readable)."""
    if not steps:
        raise PlanRevisionInvalid("The new plan has no steps.")
    if len(steps) > max_steps:
        raise PlanRevisionInvalid(
            f"The new plan has {len(steps)} steps; the limit is {max_steps}."
        )
    carriable = carriable_steps(checkpoint)
    validated: list[RevisionStep] = []
    total_chars = 0
    seen_carried: set[int] = set()
    last_carried = 0
    in_prefix = True
    for num, step in enumerate(steps, start=1):
        description = step.description.strip()
        expected = step.expected_result.strip()
        if not description:
            raise PlanRevisionInvalid(f"Step {num} has an empty description.")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise PlanRevisionInvalid(
                f"Step {num}'s description is longer than "
                f"{MAX_DESCRIPTION_CHARS} characters."
            )
        if len(expected) > MAX_EXPECTED_RESULT_CHARS:
            raise PlanRevisionInvalid(
                f"Step {num}'s expected result is longer than "
                f"{MAX_EXPECTED_RESULT_CHARS} characters."
            )
        total_chars += len(description) + len(expected)
        carried = step.carried_from
        if carried is not None:
            if not in_prefix:
                raise PlanRevisionInvalid(
                    f"Step {num} is carried after a step that will run. Carried "
                    "steps must come first."
                )
            if carried not in carriable:
                raise PlanRevisionInvalid(
                    f"Step {carried} cannot be carried: its final attempt did not "
                    "pass. Carriable steps: "
                    f"{_format_step_list(sorted(carriable))}."
                )
            if carried in seen_carried:
                raise PlanRevisionInvalid(
                    f"Earlier step {carried} is carried more than once."
                )
            if carried <= last_carried:
                raise PlanRevisionInvalid(
                    "Carried steps must keep the earlier plan's order "
                    f"(step {carried} comes after step {last_carried})."
                )
            seen_carried.add(carried)
            last_carried = carried
        else:
            in_prefix = False
        validated.append(
            RevisionStep(
                description=description,
                expected_result=expected,
                timeout_seconds=resolve_timeout_seconds(step.raw_timeout, bounds),
                carried_from=carried,
            )
        )
    if total_chars > MAX_TOTAL_TEXT_CHARS:
        raise PlanRevisionInvalid(
            f"The new plan's text is {total_chars} characters; the limit is "
            f"{MAX_TOTAL_TEXT_CHARS}. Write shorter steps."
        )
    resume_step = len(seen_carried) + 1
    if resume_step > len(validated):
        raise PlanRevisionInvalid(
            "Every step is carried, so nothing would run after Continue. Add at "
            "least one step that runs (for example a final check)."
        )
    return ValidatedRevision(steps=tuple(validated), resume_step=resume_step)


def apply_plan_revision(
    checkpoint: OrchestratorCheckpoint,
    validated: ValidatedRevision,
    *,
    revision_id: int,
    help_request_id: int,
    reason: str,
) -> OrchestratorCheckpoint:
    """The checkpoint with the new plan, carried passes and the earlier plan recorded.

    The caller validated ``validated`` against this very checkpoint.
    """
    carriable = carriable_steps(checkpoint)
    carried_attempts: list[SubTaskAttempt] = []
    mapping: list[tuple[int, int]] = []
    for num, step in enumerate(validated.steps, start=1):
        if step.carried_from is None:
            continue
        source = carriable[step.carried_from]
        mapping.append((num, step.carried_from))
        carried_attempts.append(
            SubTaskAttempt(
                step_num=num,
                kind="step",
                result=SubTaskResult(
                    status="pass", summary=source.result.summary, iterations=0
                ),
                duration_seconds=source.duration_seconds,
                carried_from=step.carried_from,
            )
        )
    version = checkpoint.plan_version + 1
    record = PlanRevisionRecord(
        version=version,
        revision_id=revision_id,
        help_request_id=help_request_id,
        reason=reason[:MAX_REASON_CHARS],
        previous_plan=tuple(
            (
                task.description[:_RECORD_STEP_TEXT_CHARS],
                task.expected_result[:_RECORD_STEP_TEXT_CHARS],
            )
            for task in checkpoint.plan
        ),
        earlier_attempts=tuple(
            dataclasses.replace(
                attempt,
                result=dataclasses.replace(attempt.result, actions_digest=""),
            )
            for attempt in checkpoint.attempts
        ),
        earlier_decisions=checkpoint.decisions,
        mapping=tuple(mapping),
        paused_step=checkpoint.next_step,
        paused_digest=_paused_digest(checkpoint)[:_RECORD_PAUSED_DIGEST_CHARS],
        resume_step=validated.resume_step,
    )
    return dataclasses.replace(
        checkpoint,
        plan=tuple(
            SubTask(
                description=step.description,
                expected_result=step.expected_result,
                timeout_seconds=step.timeout_seconds,
            )
            for step in validated.steps
        ),
        attempts=tuple(carried_attempts),
        decisions=(),
        next_step=validated.resume_step,
        plan_version=version,
        plan_revisions=(checkpoint.plan_revisions + (record,))[
            -MAX_PLAN_REVISION_RECORDS:
        ],
    )


def build_plan_draft_view(
    *,
    revision_id: int,
    reason: str,
    previous_steps: Sequence[PlanStepSnapshot],
    steps: Sequence[RevisionStep],
) -> PlanDraftView:
    carried_to = {
        step.carried_from: num
        for num, step in enumerate(steps, start=1)
        if step.carried_from is not None
    }
    carried_count = len(carried_to)
    resume_step = carried_count + 1
    old_rows = tuple(
        DraftOldRow(
            num=num,
            description=snapshot.description,
            status=snapshot.status,
            status_label=_OLD_STATUS_LABELS[snapshot.status],
            carried_to=carried_to.get(num),
        )
        for num, snapshot in enumerate(previous_steps, start=1)
    )
    new_rows: list[DraftNewRow] = []
    for num, step in enumerate(steps, start=1):
        state: NewStepState
        if step.carried_from is not None:
            state = "carried"
            label = f"Passed · carried from step {step.carried_from}"
        elif num == resume_step:
            state, label = "resume", "Resumes here"
        else:
            state, label = "run", "Will run"
        new_rows.append(
            DraftNewRow(
                num=num,
                description=step.description,
                expected_result=step.expected_result,
                carried_from=step.carried_from,
                state=state,
                state_label=label,
            )
        )
    return PlanDraftView(
        revision_id=revision_id,
        reason=reason,
        resume_step=resume_step,
        carried_count=carried_count,
        old_rows=old_rows,
        new_rows=tuple(new_rows),
    )


def attempt_origin_label(
    attempt: SubTaskAttempt, revisions: Sequence[PlanRevisionRecord]
) -> str:
    """ "Passed under the earlier plan as step k: <description>" for a carried attempt.

    Empty for an attempt that was not carried.
    """
    if attempt.carried_from is None:
        return ""
    old_description = ""
    if revisions:
        previous_plan = revisions[-1].previous_plan
        if 1 <= attempt.carried_from <= len(previous_plan):
            old_description = previous_plan[attempt.carried_from - 1][0]
    label = f"Passed under the earlier plan as step {attempt.carried_from}"
    return f"{label}: {old_description}" if old_description else label


# JSON forms stored on ``projects.CasePlanRevision`` (steps / previous_steps).


def revision_steps_to_json(steps: Sequence[RevisionStep]) -> list[dict[str, object]]:
    return [
        {
            "description": step.description,
            "expected_result": step.expected_result,
            "timeout_seconds": step.timeout_seconds,
            "carried_from": step.carried_from,
        }
        for step in steps
    ]


def revision_steps_from_json(value: object) -> tuple[RevisionStep, ...]:
    """Lenient: malformed entries are skipped (the row was written by the server)."""
    if not isinstance(value, list):
        return ()
    steps: list[RevisionStep] = []
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, object], raw)
        description, expected = item.get("description"), item.get("expected_result")
        timeout, carried = item.get("timeout_seconds"), item.get("carried_from")
        if not isinstance(description, str) or not isinstance(expected, str):
            continue
        steps.append(
            RevisionStep(
                description=description,
                expected_result=expected,
                timeout_seconds=_int_or(timeout, 0),
                carried_from=(
                    carried
                    if isinstance(carried, int) and not isinstance(carried, bool)
                    else None
                ),
            )
        )
    return tuple(steps)


def snapshots_to_json(
    snapshots: Sequence[PlanStepSnapshot],
) -> list[dict[str, object]]:
    return [
        {
            "description": snapshot.description,
            "expected_result": snapshot.expected_result,
            "status": snapshot.status,
        }
        for snapshot in snapshots
    ]


def snapshots_from_json(value: object) -> tuple[PlanStepSnapshot, ...]:
    if not isinstance(value, list):
        return ()
    snapshots: list[PlanStepSnapshot] = []
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, object], raw)
        description, expected = item.get("description"), item.get("expected_result")
        status = item.get("status")
        if not isinstance(description, str) or not isinstance(expected, str):
            continue
        snapshots.append(
            PlanStepSnapshot(
                description=description,
                expected_result=expected,
                status=(
                    cast(OldStepStatus, status)
                    if status in _OLD_STATUS_LABELS
                    else "not_run"
                ),
            )
        )
    return tuple(snapshots)


def _paused_digest(checkpoint: OrchestratorCheckpoint) -> str:
    """The digest of the last failed step attempt at the cursor, if any."""
    for attempt in reversed(checkpoint.attempts):
        if (
            attempt.kind == "step"
            and attempt.step_num == checkpoint.next_step
            and attempt.result.status != "pass"
        ):
            return attempt.result.actions_digest
    return ""


def _format_step_list(steps: Sequence[int]) -> str:
    return ", ".join(str(step) for step in steps) if steps else "none"


def _int_or(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value
