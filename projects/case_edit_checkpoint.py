"""Admin step edits applied to a case's execution checkpoint (pure, no query).

An admin rewords a step of the current plan (description, expected result),
flips the outcome of a step that ran (pass/fail of its final ``kind="step"``
attempt) and rewrites its result text, failure text and decision note. The
transform round-trips through ``agents.checkpoint`` and changes only those
fields: ``next_step``, ``phase``, ``help``, ``person``, ``plan_revisions`` and
every attempt's origin (``carried_from``, ``attempt_id``, ``performed_by``,
``person_step_id``) stay as they are.

A step's final attempt follows ``build_case_step_progress``: the step's last
``kind="step"`` attempt. Its decision is the step's latest ``StepDecision``;
the case page shows it only while that final attempt failed, but the editor
offers it whenever it exists, so flipping a passed step to failed never
surfaces a note the admin could not see or edit. Nothing is ever created: a
summary, failure text or decision note on a step that has no final attempt
(or no decision) is ignored when blank and refused otherwise.

Also: the editor's base key (``edit_base_key``), the optimistic-concurrency
token every edit form carries.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from agents.checkpoint import (
    checkpoint_from_json,
    checkpoint_to_json,
    final_step_indices,
)
from agents.types import (
    OrchestratorCheckpoint,
    StepDecision,
    SubTask,
    SubTaskAttempt,
)
from projects.case_result_fingerprint import result_fingerprint
from projects.exceptions import CaseEditError
from projects.models import TestRunTestCase

# Display cap for each ``before`` / ``after`` text of an audit diff.
CHANGE_TEXT_MAX_CHARS = 500

OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
_OUTCOMES: frozenset[str] = frozenset({OUTCOME_PASS, OUTCOME_FAIL})

# ``FieldChange.field`` values written by this module.
FIELD_STEP_DESCRIPTION = "step_description"
FIELD_STEP_EXPECTED = "step_expected_result"
FIELD_STEP_OUTCOME = "step_outcome"
FIELD_STEP_SUMMARY = "step_summary"
FIELD_STEP_ERROR = "step_error"
FIELD_STEP_DECISION = "step_decision"

STEP_FIELD_LABELS: dict[str, str] = {
    FIELD_STEP_DESCRIPTION: "Step wording",
    FIELD_STEP_EXPECTED: "Expected result",
    FIELD_STEP_OUTCOME: "Step outcome",
    FIELD_STEP_SUMMARY: "Step result",
    FIELD_STEP_ERROR: "Failure / evidence",
    FIELD_STEP_DECISION: "Decision note",
}

_NO_CHECKPOINT = "This case has no recorded steps to edit."
_PLAN_MISMATCH = "The steps changed since you opened the editor. Reload the page."


@dataclass(frozen=True)
class StepEdit:
    """The admin's cleaned values for one plan step."""

    step_num: int
    description: str
    expected_result: str
    # "pass" / "fail"; None = unchanged, or the step never ran.
    outcome: str | None
    summary: str
    error: str
    decision_reason: str


@dataclass(frozen=True)
class StepRow:
    """One plan step as the editor shows it (current values)."""

    step_num: int
    description: str
    expected_result: str
    has_final_attempt: bool
    # "pass" / "fail"; "" when the step never ran.
    outcome: str
    summary: str
    error: str
    # The step's latest decision, editable whenever one exists (the case page
    # shows it only while the final attempt failed).
    has_decision: bool
    decision_reason: str
    final_attempt_id: str


@dataclass(frozen=True)
class FieldChange:
    """One field's before/after in an audit row; texts capped for display."""

    field: str
    step_num: int | None
    before: str
    after: str

    def to_json(self) -> dict[str, object]:
        return {
            "field": self.field,
            "step_num": self.step_num,
            "before": self.before,
            "after": self.after,
        }


def cap_change_text(text: str) -> str:
    """``text`` cut to ``CHANGE_TEXT_MAX_CHARS`` (with an ellipsis) for display."""
    if len(text) <= CHANGE_TEXT_MAX_CHARS:
        return text
    return text[: CHANGE_TEXT_MAX_CHARS - 1] + "…"


def field_change(
    field: str, step_num: int | None, before: str, after: str
) -> FieldChange:
    """A ``FieldChange`` with both texts capped for display."""
    return FieldChange(
        field=field,
        step_num=step_num,
        before=cap_change_text(before),
        after=cap_change_text(after),
    )


def step_rows(checkpoint_json: object) -> list[StepRow]:
    """The current plan's steps; empty without a usable checkpoint."""
    checkpoint = checkpoint_from_json(checkpoint_json)
    if checkpoint is None:
        return []
    final_index = final_step_indices(checkpoint.attempts)
    rows: list[StepRow] = []
    for num, task in enumerate(checkpoint.plan, 1):
        final = _final_attempt(checkpoint, final_index, num)
        decision = _latest_decision(checkpoint, num)
        rows.append(
            StepRow(
                step_num=num,
                description=task.description,
                expected_result=task.expected_result,
                has_final_attempt=final is not None,
                outcome=final.result.status if final is not None else "",
                summary=final.result.summary if final is not None else "",
                error=(final.result.error or "") if final is not None else "",
                has_decision=decision is not None,
                decision_reason=decision.reason if decision is not None else "",
                final_attempt_id=final.attempt_id if final is not None else "",
            )
        )
    return rows


def apply_step_edits(
    checkpoint_json: object, edits: Sequence[StepEdit]
) -> tuple[dict[str, object], list[FieldChange]]:
    """Apply ``edits`` (one per plan step, in order) to the checkpoint.

    Returns the new checkpoint JSON and the field diff. When nothing changed,
    the JSON is ``checkpoint_json`` as it was (not re-serialised) and the diff
    is empty. Raises ``CaseEditError`` (user-safe) for an edit list that does
    not match the plan, an unknown outcome, an outcome on a step that never
    ran, text for an attempt or decision that does not exist, or a blank step
    wording.
    """
    checkpoint = checkpoint_from_json(checkpoint_json)
    if checkpoint is None:
        if edits:
            raise CaseEditError(_NO_CHECKPOINT)
        return _unchanged(checkpoint_json), []
    if [edit.step_num for edit in edits] != list(range(1, len(checkpoint.plan) + 1)):
        raise CaseEditError(_PLAN_MISMATCH)

    final_index = final_step_indices(checkpoint.attempts)
    plan = list(checkpoint.plan)
    attempts = list(checkpoint.attempts)
    decisions = list(checkpoint.decisions)
    changes: list[FieldChange] = []
    for edit in edits:
        num = edit.step_num
        plan[num - 1] = _edit_task(plan[num - 1], edit, changes)
        index = final_index.get(num)
        final = attempts[index] if index is not None else None
        decision_index = _latest_decision_index(checkpoint, num)
        if final is None or index is None:
            _refuse_without_attempt(edit)
        else:
            attempts[index] = _edit_attempt(final, edit, changes)
        if decision_index is None:
            if edit.decision_reason.strip():
                raise CaseEditError(f"Step {num} has no decision note to edit.")
        else:
            decisions[decision_index] = _edit_decision(
                decisions[decision_index], edit, changes
            )

    if not changes:
        return _unchanged(checkpoint_json), []
    edited = dataclasses.replace(
        checkpoint,
        plan=tuple(plan),
        attempts=tuple(attempts),
        decisions=tuple(decisions),
    )
    return checkpoint_to_json(edited), changes


def edit_base_key(
    pivot: TestRunTestCase, screenshots: Sequence[tuple[int, str]]
) -> str:
    """sha256 of the case's editable state; changes whenever an edit would.

    Covers the result fingerprint, ``failed_by``, the full checkpoint and every
    screenshot's ``(id, evidence_override)``. ``screenshots`` may be in any
    order.
    """
    canonical = json.dumps(
        [
            result_fingerprint(pivot),
            pivot.failed_by,
            pivot.execution_checkpoint,
            sorted([shot_id, override] for shot_id, override in screenshots),
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unchanged(checkpoint_json: object) -> dict[str, object]:
    if isinstance(checkpoint_json, dict):
        return dict(checkpoint_json)
    return {}


def _final_attempt(
    checkpoint: OrchestratorCheckpoint, final_index: dict[int, int], num: int
) -> SubTaskAttempt | None:
    index = final_index.get(num)
    return checkpoint.attempts[index] if index is not None else None


def _latest_decision_index(checkpoint: OrchestratorCheckpoint, num: int) -> int | None:
    """The index of the step's latest decision, whatever its final outcome."""
    for index in range(len(checkpoint.decisions) - 1, -1, -1):
        if checkpoint.decisions[index].step_num == num:
            return index
    return None


def _latest_decision(
    checkpoint: OrchestratorCheckpoint, num: int
) -> StepDecision | None:
    index = _latest_decision_index(checkpoint, num)
    return checkpoint.decisions[index] if index is not None else None


def _edit_task(task: SubTask, edit: StepEdit, changes: list[FieldChange]) -> SubTask:
    if not edit.description.strip():
        raise CaseEditError(f"Step {edit.step_num} needs its wording.")
    if edit.description != task.description:
        changes.append(
            field_change(
                FIELD_STEP_DESCRIPTION,
                edit.step_num,
                task.description,
                edit.description,
            )
        )
    if edit.expected_result != task.expected_result:
        changes.append(
            field_change(
                FIELD_STEP_EXPECTED,
                edit.step_num,
                task.expected_result,
                edit.expected_result,
            )
        )
    return dataclasses.replace(
        task, description=edit.description, expected_result=edit.expected_result
    )


def _refuse_without_attempt(edit: StepEdit) -> None:
    """A step that never ran takes no outcome and no result texts."""
    if edit.outcome is not None:
        raise CaseEditError(
            f"Step {edit.step_num} never ran, so its outcome cannot be set."
        )
    if edit.summary.strip() or edit.error.strip():
        raise CaseEditError(
            f"Step {edit.step_num} never ran, so it has no result to edit."
        )


def _edit_attempt(
    final: SubTaskAttempt, edit: StepEdit, changes: list[FieldChange]
) -> SubTaskAttempt:
    num = edit.step_num
    result = final.result
    status: Literal["pass", "fail"] = result.status
    if edit.outcome is not None:
        if edit.outcome not in _OUTCOMES:
            raise CaseEditError(f"Choose Passed or Failed for step {num}.")
        if edit.outcome != result.status:
            changes.append(
                field_change(FIELD_STEP_OUTCOME, num, result.status, edit.outcome)
            )
            status = "pass" if edit.outcome == OUTCOME_PASS else "fail"
    if edit.summary != result.summary:
        changes.append(
            field_change(FIELD_STEP_SUMMARY, num, result.summary, edit.summary)
        )
    before_error = result.error or ""
    error: str | None = result.error
    if edit.error != before_error:
        changes.append(field_change(FIELD_STEP_ERROR, num, before_error, edit.error))
        error = edit.error or None
    return dataclasses.replace(
        final,
        result=dataclasses.replace(
            result, status=status, summary=edit.summary, error=error
        ),
    )


def _edit_decision(
    decision: StepDecision, edit: StepEdit, changes: list[FieldChange]
) -> StepDecision:
    if edit.decision_reason == decision.reason:
        return decision
    changes.append(
        field_change(
            FIELD_STEP_DECISION, edit.step_num, decision.reason, edit.decision_reason
        )
    )
    return dataclasses.replace(decision, reason=edit.decision_reason)
