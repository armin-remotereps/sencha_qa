"""(De)serialization of the orchestrator checkpoint stored on a run case.

Pure: no ORM. A checkpoint that does not parse means "plan from scratch".

New keys must stay optional and default when missing, with ``CHECKPOINT_VERSION``
unchanged: a bump makes every stored checkpoint (and, on rollback, every new one)
unusable, so every case would resume from step 1. Older parsers ignore unknown
keys, which keeps rollbacks safe.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Literal, cast

from agents.types import (
    OrchestratorCheckpoint,
    OrchestratorPhase,
    StepDecision,
    SubTask,
    SubTaskAttempt,
    SubTaskResult,
)
from controller_client.protocol import BrowserName

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1

_ATTEMPT_KINDS = ("step", "recovery", "resume")
_RESULT_STATUSES = ("pass", "fail")
_DECISION_ACTIONS = ("continue", "recover", "stop")
_PHASES = ("step", "evaluating", "recovery", "resume", "verdict")


class _MalformedCheckpoint(Exception):
    pass


def checkpoint_to_json(checkpoint: OrchestratorCheckpoint) -> dict[str, object]:
    return {
        "version": CHECKPOINT_VERSION,
        "plan": [
            {
                "description": task.description,
                "expected_result": task.expected_result,
                "timeout_seconds": task.timeout_seconds,
            }
            for task in checkpoint.plan
        ],
        "attempts": [
            {
                "step_num": attempt.step_num,
                "kind": attempt.kind,
                "status": attempt.result.status,
                "summary": attempt.result.summary,
                "iterations": attempt.result.iterations,
                "error": attempt.result.error,
                "actions_digest": attempt.result.actions_digest,
                "duration_seconds": attempt.duration_seconds,
            }
            for attempt in checkpoint.attempts
        ],
        "next_step": checkpoint.next_step,
        "browser": checkpoint.browser.value,
        "total_iterations": checkpoint.total_iterations,
        "decisions": [
            {
                "step_num": decision.step_num,
                "action": decision.action,
                "reason": decision.reason,
            }
            for decision in checkpoint.decisions
        ],
        "phase": checkpoint.phase,
        "phase_started_at": (
            checkpoint.phase_started_at.isoformat()
            if checkpoint.phase_started_at
            else None
        ),
    }


def checkpoint_from_json(data: object) -> OrchestratorCheckpoint | None:
    """Parse a stored checkpoint; ``None`` when empty, outdated or malformed."""
    if not data:
        return None
    try:
        return _parse(data)
    except _MalformedCheckpoint as exc:
        logger.warning("Ignoring unusable execution checkpoint: %s", exc)
        return None


def truncate_checkpoint(
    checkpoint: OrchestratorCheckpoint, from_step: int
) -> OrchestratorCheckpoint:
    """Forget every attempt and decision at or after ``from_step``; resume there."""
    return dataclasses.replace(
        checkpoint,
        attempts=tuple(a for a in checkpoint.attempts if a.step_num < from_step),
        decisions=tuple(d for d in checkpoint.decisions if d.step_num < from_step),
        next_step=from_step,
    )


def _parse(data: object) -> OrchestratorCheckpoint:
    root = _as_dict(data, "checkpoint")
    if root.get("version") != CHECKPOINT_VERSION:
        raise _MalformedCheckpoint(f"unsupported version {root.get('version')!r}")
    plan = tuple(
        SubTask(
            description=_as_str(item.get("description"), "description"),
            expected_result=_as_str(item.get("expected_result"), "expected_result"),
            timeout_seconds=_as_int(item.get("timeout_seconds"), "timeout_seconds"),
        )
        for item in (_as_dict(raw, "plan item") for raw in _as_list(root, "plan"))
    )
    if not plan:
        raise _MalformedCheckpoint("empty plan")
    attempts = tuple(
        _parse_attempt(_as_dict(raw, "attempt")) for raw in _as_list(root, "attempts")
    )
    next_step = _as_int(root.get("next_step"), "next_step")
    if not 1 <= next_step <= len(plan) + 1:
        raise _MalformedCheckpoint(f"next_step {next_step} out of range")
    try:
        browser = BrowserName(_as_str(root.get("browser"), "browser"))
    except ValueError as exc:
        raise _MalformedCheckpoint("unknown browser") from exc
    return OrchestratorCheckpoint(
        plan=plan,
        attempts=attempts,
        next_step=next_step,
        browser=browser,
        total_iterations=_as_int(root.get("total_iterations"), "total_iterations"),
        decisions=_parse_decisions(root.get("decisions")),
        phase=_parse_phase(root.get("phase")),
        phase_started_at=_parse_datetime(root.get("phase_started_at")),
    )


def _parse_attempt(item: dict[str, object]) -> SubTaskAttempt:
    kind = item.get("kind")
    status = item.get("status")
    if kind not in _ATTEMPT_KINDS or status not in _RESULT_STATUSES:
        raise _MalformedCheckpoint("bad attempt kind or status")
    error = item.get("error")
    if error is not None and not isinstance(error, str):
        raise _MalformedCheckpoint("bad attempt error")
    return SubTaskAttempt(
        step_num=_as_int(item.get("step_num"), "step_num"),
        kind=cast(Literal["step", "recovery", "resume"], kind),
        result=SubTaskResult(
            status=cast(Literal["pass", "fail"], status),
            summary=_as_str(item.get("summary"), "summary"),
            iterations=_as_int(item.get("iterations"), "iterations"),
            error=error,
            actions_digest=_parse_digest(item.get("actions_digest")),
        ),
        duration_seconds=_parse_duration(item.get("duration_seconds")),
    )


# The display-only keys below never make a checkpoint unusable: a bad value
# falls back to its default, so Resume keeps the step cursor.


def _parse_decisions(value: object) -> tuple[StepDecision, ...]:
    if not isinstance(value, list):
        return ()
    decisions: list[StepDecision] = []
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, object], raw)
        step_num, action, reason = (
            item.get("step_num"),
            item.get("action"),
            item.get("reason"),
        )
        if (
            isinstance(step_num, int)
            and not isinstance(step_num, bool)
            and action in _DECISION_ACTIONS
            and isinstance(reason, str)
        ):
            decisions.append(
                StepDecision(
                    step_num=step_num,
                    action=cast(Literal["continue", "recover", "stop"], action),
                    reason=reason,
                )
            )
    return tuple(decisions)


def _parse_phase(value: object) -> OrchestratorPhase:
    if value not in _PHASES:
        return "step"
    return cast(OrchestratorPhase, value)


def _parse_digest(value: object) -> str:
    # Absent from checkpoints written before the digest existed.
    return value if isinstance(value, str) else ""


def _parse_duration(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _as_dict(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _MalformedCheckpoint(f"{name} is not an object")
    return cast(dict[str, object], value)


def _as_list(root: dict[str, object], key: str) -> list[object]:
    value = root.get(key)
    if not isinstance(value, list):
        raise _MalformedCheckpoint(f"{key} is not a list")
    return cast(list[object], value)


def _as_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise _MalformedCheckpoint(f"{name} is not a string")
    return value


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _MalformedCheckpoint(f"{name} is not an integer")
    return value
