"""Which screenshots of a checked case are sent to TestRail as evidence.

Pure: no queries. Every screenshot is tagged with the step attempt that took it
(``TestRunScreenshot.attempt_id`` = ``SubTaskAttempt.attempt_id``), so the
attempts that caused the verdict pick their own screenshots:

- Passed: none.
- Failed: the final ``step`` attempt of every step whose final attempt failed;
  the last ``per_step`` screenshots of each, at most ``max_shots`` in all,
  keeping the latest steps.
- Blocked (a person's decision): the attempt the case stopped on. That is the
  attempt a fault interrupted at the checkpoint cursor (its screenshots are the
  newest and its id was never recorded), else the last failed attempt of any
  kind at the cursor, else the Failed rule's attempts.
- Fallback, when no causing attempt has a tagged screenshot (legacy data, a
  fault before the attempt was recorded, a person-made Failed where every step
  passed, no usable checkpoint): the case's last ``per_step`` screenshots.

Apart from the interrupted attempt at the cursor, an attempt id counts only
once the attempt is recorded in the checkpoint, so screenshots of truncated or
earlier-plan attempts never match.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agents.checkpoint import final_step_indices
from agents.types import OrchestratorCheckpoint
from projects.models import TestRunTestCaseStatus

# OmniParser annotated overlays are debugging images, never evidence.
OVERLAY_TOOL_NAME = "controller_omniparser"


@dataclass(frozen=True)
class EvidenceShot:
    """One candidate screenshot (overlays excluded), in ``(created_at, id)`` order."""

    id: int
    attempt_id: str
    step_num: int | None


def select_evidence(
    *,
    status: str,
    checkpoint: OrchestratorCheckpoint | None,
    shots: Sequence[EvidenceShot],
    per_step: int,
    max_shots: int,
) -> list[int]:
    """Ids of the screenshots to send, in the order of ``shots``."""
    if status == TestRunTestCaseStatus.FAILED:
        causing = _failed_attempt_ids(checkpoint)
    elif status == TestRunTestCaseStatus.BLOCKED:
        causing = _blocked_attempt_ids(checkpoint, shots)
    else:
        return []
    selected = _shots_of_attempts(causing, shots, per_step, max_shots)
    if not selected:
        selected = _last(list(shots), min(per_step, max_shots))
    return [shot.id for shot in selected]


def _failed_attempt_ids(checkpoint: OrchestratorCheckpoint | None) -> list[str]:
    """Final ``step`` attempts that failed, in the order they ran."""
    if checkpoint is None:
        return []
    attempts = checkpoint.attempts
    return [
        attempts[index].attempt_id
        for index in sorted(final_step_indices(attempts).values())
        if attempts[index].result.status == "fail"
    ]


def _blocked_attempt_ids(
    checkpoint: OrchestratorCheckpoint | None, shots: Sequence[EvidenceShot]
) -> list[str]:
    """The attempt the case stopped on, else the Failed rule's attempts."""
    if checkpoint is None:
        return []
    interrupted = _interrupted_attempt_id(checkpoint, shots)
    if interrupted:
        return [interrupted]
    for attempt in reversed(checkpoint.attempts):
        if attempt.step_num == checkpoint.next_step and attempt.result.status == "fail":
            return [attempt.attempt_id]
    return _failed_attempt_ids(checkpoint)


def _interrupted_attempt_id(
    checkpoint: OrchestratorCheckpoint, shots: Sequence[EvidenceShot]
) -> str:
    """The attempt a fault cut short at the cursor: the newest tagged shot's,
    when it belongs to the cursor step and was never recorded."""
    tagged = [shot for shot in shots if shot.attempt_id]
    if not tagged:
        return ""
    newest = tagged[-1]
    recorded = {attempt.attempt_id for attempt in checkpoint.attempts}
    if newest.step_num == checkpoint.next_step and newest.attempt_id not in recorded:
        return newest.attempt_id
    return ""


def _shots_of_attempts(
    attempt_ids: Sequence[str],
    shots: Sequence[EvidenceShot],
    per_step: int,
    max_shots: int,
) -> list[EvidenceShot]:
    """The last ``per_step`` shots of each attempt, capped keeping the latest."""
    by_attempt: dict[str, list[EvidenceShot]] = {}
    for shot in shots:
        if shot.attempt_id:
            by_attempt.setdefault(shot.attempt_id, []).append(shot)
    picked: list[EvidenceShot] = []
    for attempt_id in attempt_ids:
        if attempt_id:
            picked.extend(_last(by_attempt.get(attempt_id, []), per_step))
    chosen = {shot.id for shot in _last(picked, max_shots)}
    return [shot for shot in shots if shot.id in chosen]


def _last(items: list[EvidenceShot], count: int) -> list[EvidenceShot]:
    return items[-count:] if count > 0 else []
