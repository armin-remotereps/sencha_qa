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

Manual run cases (``select_manual_evidence``) attach every screenshot of
their current hand-in, Passed included, capped at ``max_shots``: the case
screenshot and failed steps survive the cap first, then passed steps; the
chosen ids come back in step order, the case screenshot first (D7).

Apart from the interrupted attempt at the cursor, an attempt id counts only
once the attempt is recorded in the checkpoint, so screenshots of truncated or
earlier-plan attempts never match.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from agents.checkpoint import final_step_indices
from agents.types import OrchestratorCheckpoint
from controller_client.protocol import ManualShotKind
from projects.manual_run_policy import MANUAL_SHOT_TOOL_NAMES
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


# Which manual shots survive the cap first: the case screenshot, then failed
# steps, then passed steps.
_MANUAL_PRIORITY: Final[dict[str, int]] = {
    MANUAL_SHOT_TOOL_NAMES[ManualShotKind.CASE]: 0,
    MANUAL_SHOT_TOOL_NAMES[ManualShotKind.FAIL]: 1,
    MANUAL_SHOT_TOOL_NAMES[ManualShotKind.PASS]: 2,
}
_MANUAL_EVIDENCE_STATUSES: Final = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
)


@dataclass(frozen=True)
class ManualEvidenceShot:
    """One screenshot of a manual case's current hand-in, in ``(created_at,
    id)`` order."""

    id: int
    tool_name: str
    step_num: int | None


def select_manual_evidence(
    *, status: str, shots: Sequence[ManualEvidenceShot], max_shots: int
) -> list[int]:
    """Ids of a manual case's screenshots to send, in step order (the case
    screenshot first), chosen under the cap by priority.

    Passed, Failed and Blocked cases send them all up to ``max_shots``; a
    Needs re-test case sends none (as for every mode).
    """
    if status not in _MANUAL_EVIDENCE_STATUSES or max_shots <= 0:
        return []
    ranked = sorted(
        enumerate(shots),
        key=lambda item: (
            _MANUAL_PRIORITY.get(item[1].tool_name, len(_MANUAL_PRIORITY)),
            item[1].step_num if item[1].step_num is not None else 0,
            item[0],
        ),
    )
    chosen = [shot for _, shot in ranked[:max_shots]]
    # Returned in step order: the case screenshot first, then by step.
    order = {shot.id: index for index, shot in enumerate(shots)}
    chosen.sort(
        key=lambda shot: (
            shot.step_num is not None,
            shot.step_num or 0,
            order[shot.id],
        )
    )
    return [shot.id for shot in chosen]
