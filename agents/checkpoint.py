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
    CheckpointHelp,
    DecisionAction,
    HelpSource,
    OrchestratorCheckpoint,
    OrchestratorPhase,
    PlanRevisionRecord,
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
_DECISION_ACTIONS = ("continue", "recover", "stop", "need_help")
_PHASES = ("step", "evaluating", "recovery", "resume", "verdict", "awaiting_help")
_HELP_SOURCES = ("human", "auto_fix")
_HELP_KEY = "help"
_PLAN_VERSION_KEY = "plan_version"
_PLAN_REVISIONS_KEY = "plan_revisions"
# Only the newest earlier plans are kept (display and prompt data).
MAX_PLAN_REVISION_RECORDS = 5


class _MalformedCheckpoint(Exception):
    pass


def checkpoint_to_json(checkpoint: OrchestratorCheckpoint) -> dict[str, object]:
    data = _checkpoint_body(checkpoint)
    if checkpoint.help is not None:
        data[_HELP_KEY] = help_to_json(checkpoint.help)
    if checkpoint.plan_version:
        data[_PLAN_VERSION_KEY] = checkpoint.plan_version
    if checkpoint.plan_revisions:
        data[_PLAN_REVISIONS_KEY] = [
            _revision_to_json(record)
            for record in checkpoint.plan_revisions[-MAX_PLAN_REVISION_RECORDS:]
        ]
    return data


def _attempt_to_json(attempt: SubTaskAttempt) -> dict[str, object]:
    data: dict[str, object] = {
        "step_num": attempt.step_num,
        "kind": attempt.kind,
        "status": attempt.result.status,
        "summary": attempt.result.summary,
        "iterations": attempt.result.iterations,
        "error": attempt.result.error,
        "actions_digest": attempt.result.actions_digest,
        "duration_seconds": attempt.duration_seconds,
    }
    if attempt.carried_from is not None:
        data["carried_from"] = attempt.carried_from
    return data


def _decision_to_json(decision: StepDecision) -> dict[str, object]:
    return {
        "step_num": decision.step_num,
        "action": decision.action,
        "reason": decision.reason,
    }


def _revision_to_json(record: PlanRevisionRecord) -> dict[str, object]:
    return {
        "version": record.version,
        "revision_id": record.revision_id,
        "help_request_id": record.help_request_id,
        "reason": record.reason,
        "previous_plan": [
            {"description": description, "expected_result": expected}
            for description, expected in record.previous_plan
        ],
        "earlier_attempts": [
            _attempt_to_json(attempt) for attempt in record.earlier_attempts
        ],
        "earlier_decisions": [
            _decision_to_json(decision) for decision in record.earlier_decisions
        ],
        "mapping": [[new_step, old_step] for new_step, old_step in record.mapping],
        "paused_step": record.paused_step,
        "paused_digest": record.paused_digest,
        "resume_step": record.resume_step,
    }


def _checkpoint_body(checkpoint: OrchestratorCheckpoint) -> dict[str, object]:
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
        "attempts": [_attempt_to_json(attempt) for attempt in checkpoint.attempts],
        "next_step": checkpoint.next_step,
        "browser": checkpoint.browser.value,
        "total_iterations": checkpoint.total_iterations,
        "decisions": [_decision_to_json(decision) for decision in checkpoint.decisions],
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


def help_to_json(help: CheckpointHelp) -> dict[str, object]:
    return {
        "step_num": help.step_num,
        "request_id": help.request_id,
        "note": help.note,
        "source": help.source,
        "continue_pending": help.continue_pending,
        "ask": help.ask,
        "auto_fix_summary": help.auto_fix_summary,
        "auto_fix_fixed": help.auto_fix_fixed,
        "fault": help.fault,
    }


def read_checkpoint_help(checkpoint_json: object) -> CheckpointHelp | None:
    """The ``help`` key of a stored checkpoint; ``None`` when absent or malformed.

    Reads only that key, so callers need not parse the whole checkpoint.
    """
    if not isinstance(checkpoint_json, dict):
        return None
    return _parse_help(cast(dict[str, object], checkpoint_json).get(_HELP_KEY))


def with_help(checkpoint_json: object, help: CheckpointHelp) -> dict[str, object]:
    """A copy of a stored checkpoint with its ``help`` key set to ``help``.

    A value that is not a non-empty object has no progress to continue from,
    so ``{}`` (plan from scratch) is returned unchanged.
    """
    if not isinstance(checkpoint_json, dict) or not checkpoint_json:
        return {}
    data = dict(cast(dict[str, object], checkpoint_json))
    data[_HELP_KEY] = help_to_json(help)
    return data


def clear_help_continue_pending(checkpoint_json: object) -> dict[str, object]:
    """A copy of a stored checkpoint whose ``help`` no longer asks to continue.

    Without a (valid) ``help`` key the checkpoint is returned as a plain copy;
    a value that is not an object becomes ``{}``.
    """
    if not isinstance(checkpoint_json, dict):
        return {}
    data = dict(cast(dict[str, object], checkpoint_json))
    help = _parse_help(data.get(_HELP_KEY))
    if help is not None and help.continue_pending:
        data[_HELP_KEY] = help_to_json(
            dataclasses.replace(help, continue_pending=False)
        )
    return data


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
        help=_parse_help(root.get(_HELP_KEY)),
        plan_version=_parse_plan_version(root.get(_PLAN_VERSION_KEY)),
        plan_revisions=_parse_revisions(root.get(_PLAN_REVISIONS_KEY)),
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
        # Absent from attempts that were not carried over a plan redesign.
        carried_from=_parse_positive_int(item.get("carried_from")),
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
                    action=cast(DecisionAction, action),
                    reason=reason,
                )
            )
    return tuple(decisions)


def _parse_phase(value: object) -> OrchestratorPhase:
    if value not in _PHASES:
        return "step"
    return cast(OrchestratorPhase, value)


def _parse_help(value: object) -> CheckpointHelp | None:
    # Absent from checkpoints of cases that never asked for help.
    if not isinstance(value, dict):
        return None
    item = cast(dict[str, object], value)
    step_num, request_id, note, source, continue_pending = (
        item.get("step_num"),
        item.get("request_id"),
        item.get("note"),
        item.get("source"),
        item.get("continue_pending"),
    )
    if isinstance(step_num, bool) or not isinstance(step_num, int):
        return None
    if source not in _HELP_SOURCES:
        return None
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        request_id = None
    # ``ask``, ``auto_fix_summary``, ``auto_fix_fixed`` and ``fault`` are absent
    # from older help keys.
    ask, auto_fix_summary = item.get("ask"), item.get("auto_fix_summary")
    fault = item.get("fault")
    return CheckpointHelp(
        step_num=step_num,
        request_id=request_id,
        note=note if isinstance(note, str) else "",
        source=cast(HelpSource, source),
        continue_pending=continue_pending is True,
        ask=ask if isinstance(ask, str) else "",
        auto_fix_summary=(
            auto_fix_summary if isinstance(auto_fix_summary, str) else ""
        ),
        auto_fix_fixed=item.get("auto_fix_fixed") is True,
        fault=fault if isinstance(fault, str) else "",
    )


def _parse_plan_version(value: object) -> int:
    return _parse_positive_int(value) or 0


def _parse_revisions(value: object) -> tuple[PlanRevisionRecord, ...]:
    # Absent from checkpoints never redesigned; a malformed record is dropped.
    if not isinstance(value, list):
        return ()
    records: list[PlanRevisionRecord] = []
    for raw in cast(list[object], value):
        try:
            records.append(_parse_revision(_as_dict(raw, "plan revision")))
        except _MalformedCheckpoint as exc:
            logger.warning("Dropping malformed plan revision record: %s", exc)
    return tuple(records[-MAX_PLAN_REVISION_RECORDS:])


def _parse_revision(item: dict[str, object]) -> PlanRevisionRecord:
    previous_plan = tuple(
        (
            _as_str(step.get("description"), "description"),
            _as_str(step.get("expected_result"), "expected_result"),
        )
        for step in (
            _as_dict(raw, "previous plan step")
            for raw in _as_list(item, "previous_plan")
        )
    )
    mapping: list[tuple[int, int]] = []
    for raw in _as_list(item, "mapping"):
        if not isinstance(raw, list) or len(cast(list[object], raw)) != 2:
            raise _MalformedCheckpoint("bad mapping pair")
        pair = cast(list[object], raw)
        mapping.append((_as_int(pair[0], "new step"), _as_int(pair[1], "old step")))
    return PlanRevisionRecord(
        version=_as_int(item.get("version"), "version"),
        revision_id=_as_int(item.get("revision_id"), "revision_id"),
        help_request_id=_as_int(item.get("help_request_id"), "help_request_id"),
        reason=_as_str(item.get("reason"), "reason"),
        previous_plan=previous_plan,
        earlier_attempts=tuple(
            _parse_attempt(_as_dict(raw, "attempt"))
            for raw in _as_list(item, "earlier_attempts")
        ),
        earlier_decisions=_parse_decisions(item.get("earlier_decisions")),
        mapping=tuple(mapping),
        paused_step=_as_int(item.get("paused_step"), "paused_step"),
        paused_digest=_parse_digest(item.get("paused_digest")),
        resume_step=_as_int(item.get("resume_step"), "resume_step"),
    )


def _parse_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


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
