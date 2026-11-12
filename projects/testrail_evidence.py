"""Which screenshots of a checked case go to TestRail as evidence.

Pure: no queries. An AI review (``testrail_evidence_review_services``)
judges a bounded set of *candidates*; only the ones it judged relevant are
attached (``select_reviewed_evidence``), except where an admin set a
screenshot's ``evidence_override`` (``exclude`` never, ``include`` always,
candidate or not) on the verdict it was set on (``effective_override``). Nothing relevant means nothing is attached: there is no walk-back to other attempts and no "last shots"
fallback for unjudged screenshots.

Agent cases (``agent_review_candidates``): every screenshot is tagged with the
step attempt that took it (``TestRunScreenshot.attempt_id`` =
``SubTaskAttempt.attempt_id``), so the attempts that caused the verdict pick
their own candidates:

- Passed, Needs re-test: none.
- Failed: the final ``step`` attempt of every step whose final attempt failed.
- Blocked (a person's decision): the attempt the case stopped on. That is the
  attempt a fault interrupted at the checkpoint cursor (its screenshots are the
  newest and its id was never recorded), else the last failed attempt of any
  kind at the cursor, else the Failed rule's attempts.
- Every shot of those attempts, keeping the newest ``max_images``. When no
  causing attempt has a tagged screenshot (legacy data, a fault before the
  attempt was recorded, a person-made Failed where every step passed, no
  usable checkpoint), the case's newest ``max_images`` screenshots.

Manual run cases (``manual_review_candidates``): every screenshot of their
current hand-in, Passed included, capped at ``max_shots``: the case
screenshot and failed steps survive the cap first, then passed steps; they
come back in step order, the case screenshot first (D7).

Apart from the interrupted attempt at the cursor, an attempt id counts only
once the attempt is recorded in the checkpoint, so screenshots of truncated or
earlier-plan attempts never match.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from agents.checkpoint import final_step_indices
from agents.services.evidence_review_prompts import EVIDENCE_REVIEW_PROMPT_VERSION
from agents.types import OrchestratorCheckpoint, SubTaskAttempt
from controller_client.protocol import ManualShotKind
from projects.manual_run_policy import MANUAL_SHOT_TOOL_NAMES
from projects.models import (
    EvidenceOverride,
    ScreenshotRelevance,
    TestRunTestCaseStatus,
)

# OmniParser annotated overlays are debugging images, never evidence.
OVERLAY_TOOL_NAME = "controller_omniparser"


@dataclass(frozen=True)
class EvidenceShot:
    """One screenshot (overlays excluded), in ``(created_at, id)`` order.

    ``attempt_id`` is "" for untagged and manual screenshots.
    """

    id: int
    attempt_id: str
    step_num: int | None
    tool_name: str = ""


@dataclass(frozen=True)
class EvidenceSelection:
    """The screenshots to attach (in candidate order) and how many candidates
    were judged not relevant (the comment's note)."""

    ids: list[int]
    omitted: int


@dataclass(frozen=True)
class ManualMark:
    """A manual step's final mark (``pass``/``fail``) and the tester's note."""

    step_num: int
    status: str
    note: str


# ============================================================================
# AGENT CANDIDATES
# ============================================================================


def causing_attempt_ids(
    *,
    status: str,
    checkpoint: OrchestratorCheckpoint | None,
    shots: Sequence[EvidenceShot],
) -> list[str]:
    """The attempts that caused a Failed or Blocked verdict, in run order."""
    if status == TestRunTestCaseStatus.FAILED:
        return _failed_attempt_ids(checkpoint)
    if status == TestRunTestCaseStatus.BLOCKED:
        return _blocked_attempt_ids(checkpoint, shots)
    return []


def agent_review_candidates(
    *,
    status: str,
    checkpoint: OrchestratorCheckpoint | None,
    shots: Sequence[EvidenceShot],
    max_images: int,
) -> list[EvidenceShot]:
    """The screenshots the AI judges for an agent case, in ``shots`` order."""
    if status not in (TestRunTestCaseStatus.FAILED, TestRunTestCaseStatus.BLOCKED):
        return []
    causing = {
        attempt_id
        for attempt_id in causing_attempt_ids(
            status=status, checkpoint=checkpoint, shots=shots
        )
        if attempt_id
    }
    of_causing = [shot for shot in shots if shot.attempt_id in causing]
    return _last(of_causing or list(shots), max_images)


def causing_attempts(
    checkpoint: OrchestratorCheckpoint | None, attempt_ids: Sequence[str]
) -> list[SubTaskAttempt]:
    """The recorded attempts among ``attempt_ids``, in ``attempt_ids`` order."""
    if checkpoint is None:
        return []
    by_id = {
        attempt.attempt_id: attempt
        for attempt in checkpoint.attempts
        if attempt.attempt_id
    }
    return [by_id[attempt_id] for attempt_id in attempt_ids if attempt_id in by_id]


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


def _last(items: list[EvidenceShot], count: int) -> list[EvidenceShot]:
    return items[-count:] if count > 0 else []


# ============================================================================
# MANUAL CANDIDATES
# ============================================================================

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


def manual_review_candidates(
    *, status: str, shots: Sequence[EvidenceShot], max_shots: int
) -> list[EvidenceShot]:
    """A manual case's current hand-in shots the AI judges, in step order (the
    case screenshot first), chosen under the cap by priority.

    Passed, Failed and Blocked cases: all up to ``max_shots``; a Needs
    re-test case: none (as for every mode).
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
    order = {shot.id: index for index, shot in enumerate(shots)}
    chosen.sort(
        key=lambda shot: (
            shot.step_num is not None,
            shot.step_num or 0,
            order[shot.id],
        )
    )
    return chosen


# ============================================================================
# REVIEW KEY AND SELECTION
# ============================================================================


def evidence_review_key(
    *,
    status: str,
    verdict_text: str,
    candidate_ids: Sequence[int],
    manual_marks: Sequence[ManualMark] = (),
) -> str:
    """SHA-256 of what a review judged: prompt version, status, verdict text,
    candidate ids in order and, for manual cases, the tester's marks and notes.

    Not the log: it cannot change while the case is checked (every log write
    reopens the case and clears Checked).
    """
    payload = {
        "v": EVIDENCE_REVIEW_PROMPT_VERSION,
        "status": status,
        "verdict": verdict_text,
        "shots": list(candidate_ids),
        "marks": [[mark.step_num, mark.status, mark.note] for mark in manual_marks],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def effective_override(
    override: str,
    override_finished_at: datetime | None,
    case_finished_at: datetime | None,
) -> str:
    """A screenshot's ``EvidenceOverride`` as it applies to the case now.

    An override belongs to the verdict it was set on, keyed by the case's
    ``finished_at`` then (``override_finished_at``): a new verdict (Resume,
    a Manual re-claim, ...) always sets a new ``finished_at``, so an older
    override no longer applies (``AUTO``). Admin result edits keep it.
    """
    if not override or override_finished_at is None or case_finished_at is None:
        return EvidenceOverride.AUTO
    if override_finished_at != case_finished_at:
        return EvidenceOverride.AUTO
    return override


def select_reviewed_evidence(
    candidates: Sequence[EvidenceShot],
    relevance: Mapping[int, str],
    *,
    overrides: Mapping[int, str],
    order: Mapping[int, int],
    manual: bool,
    per_step: int,
    max_shots: int,
) -> EvidenceSelection:
    """The screenshots to attach: the AI's relevant candidates (from the
    review's ``relevance``, shot id -> ``ScreenshotRelevance``) minus the
    admin's ``exclude`` overrides, plus every ``include`` override.

    ``overrides`` maps shot id -> ``EvidenceOverride`` for the case's shots
    that carry one and have a file, candidates or not. ``order`` ranks every
    shot of ``candidates`` and ``overrides`` by ``(created_at, id)``; ``ids``
    come back in that order. The caps apply to the AI's picks only (agent: at
    most ``per_step`` per attempt, then ``max_shots``, keeping the latest;
    manual: up to ``max_shots`` in the candidates' step order); an include is
    always attached. ``omitted`` counts the candidates the review did not
    judge relevant and the admin did not include (cap drops and excludes of
    relevant shots are not counted).
    """
    relevant = [
        shot
        for shot in candidates
        if relevance.get(shot.id) == ScreenshotRelevance.RELEVANT
    ]
    picked = _ai_picks(relevant, manual=manual, per_step=per_step, max_shots=max_shots)
    chosen = {
        shot_id
        for shot_id in picked
        if overrides.get(shot_id) != EvidenceOverride.EXCLUDE
    } | {
        shot_id
        for shot_id, override in overrides.items()
        if override == EvidenceOverride.INCLUDE
    }
    ids = sorted(chosen, key=lambda shot_id: (order.get(shot_id, 0), shot_id))
    # As before overrides: the candidates the review judged not relevant
    # (``_screenshots_note``). An include is not omitted; an exclude of a
    # relevant shot is the admin's choice, not "not relevant".
    omitted = sum(
        1
        for shot in candidates
        if relevance.get(shot.id) != ScreenshotRelevance.RELEVANT
        and overrides.get(shot.id) != EvidenceOverride.INCLUDE
    )
    return EvidenceSelection(ids=ids, omitted=omitted)


def all_candidates_overridden(
    candidates: Sequence[EvidenceShot], overrides: Mapping[int, str]
) -> bool:
    """Whether the admin decided every candidate (no AI review needed)."""
    return bool(candidates) and all(
        overrides.get(shot.id, EvidenceOverride.AUTO) != EvidenceOverride.AUTO
        for shot in candidates
    )


def _ai_picks(
    relevant: list[EvidenceShot], *, manual: bool, per_step: int, max_shots: int
) -> set[int]:
    """The relevant candidates kept under the caps."""
    if manual:
        return {shot.id for shot in relevant[:max_shots]} if max_shots > 0 else set()
    by_attempt: dict[str, list[EvidenceShot]] = {}
    for shot in relevant:
        by_attempt.setdefault(shot.attempt_id, []).append(shot)
    picked = {
        shot.id for group in by_attempt.values() for shot in _last(group, per_step)
    }
    return {
        shot.id
        for shot in _last([shot for shot in relevant if shot.id in picked], max_shots)
    }
