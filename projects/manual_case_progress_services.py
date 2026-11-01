"""Use case: the Test Runner reports the tester's step marks so far.

The marks live in the runner's spool until the hand-in; the window reports
them (debounced, and just before a pause or a discussion message) so the
manual discussion agent and the case page can see them. The report is
advisory only: the hand-in stays authoritative and never reads it.

One ``ManualCaseProgress`` row per run case, overwritten in place and fenced
by the claim token it was reported under: a reader ignores a row whose token
is not the case's current ``manual_claim_token``, so no path that ends a claim
has to clear it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from django.db import transaction
from django.utils import timezone

from agents.checkpoint import checkpoint_from_json
from controller_client.protocol import ManualProgressStepPayload, ManualStepOutcome
from projects.exceptions import ManualRunError
from projects.manual_case_lock import CASE_GONE, claim_matches
from projects.manual_run_policy import (
    MANUAL_PAUSE_STALE_CLAIM,
    MANUAL_RUN_NOT_RUNNING,
    is_claimed_manual_case,
    is_manual_run,
)
from projects.models import (
    ManualCaseProgress,
    Project,
    TestRunStatus,
    TestRunTestCase,
)

_NOT_MANUAL_CASE: Final = "This case has no manual steps."
_STEP_OUT_OF_RANGE: Final = "The progress names a step this case does not have."
_OUTCOMES: Final = frozenset(outcome.value for outcome in ManualStepOutcome)


@dataclass(frozen=True)
class ManualProgressStep:
    num: int
    # "pass" | "fail"
    outcome: str
    note: str


@dataclass(frozen=True)
class ManualProgressView:
    # The step the window showed; None when unknown.
    current_step: int | None
    # By step number.
    steps: tuple[ManualProgressStep, ...]
    reported_at: datetime


def record_manual_progress(
    *,
    project: Project,
    pivot_id: int,
    claim_token: str,
    current_step: int | None,
    steps: Sequence[ManualProgressStepPayload],
) -> None:
    """Store the runner's latest marks of the claimed case (upsert).

    Locks the case row only (no project or run lock: a report never changes
    the case). Raises ``ManualRunError`` (user-safe) when the case is not the
    claimed case of the project's running manual run, the claim token does
    not match, or a step number is beyond the case's verbatim steps.
    """
    with transaction.atomic():
        pivot = (
            TestRunTestCase.objects.select_for_update(of=("self",))
            .select_related("test_run")
            .only(
                "id",
                "status",
                "manual_claim_token",
                "execution_checkpoint",
                "test_run",
                "test_run__status",
                "test_run__help_mode",
            )
            .filter(pk=pivot_id, test_run__project_id=project.pk)
            .first()
        )
        if pivot is None or not is_manual_run(pivot.test_run):
            raise ManualRunError(CASE_GONE)
        if pivot.test_run.status != TestRunStatus.STARTED:
            raise ManualRunError(MANUAL_RUN_NOT_RUNNING)
        if not is_claimed_manual_case(pivot.test_run, pivot) or not claim_matches(
            pivot, claim_token
        ):
            raise ManualRunError(MANUAL_PAUSE_STALE_CLAIM)
        checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
        if checkpoint is None or not checkpoint.manual:
            raise ManualRunError(_NOT_MANUAL_CASE)
        step_count = len(checkpoint.plan)
        if any(step.num > step_count for step in steps) or (
            current_step is not None and current_step > step_count
        ):
            raise ManualRunError(_STEP_OUT_OF_RANGE)
        ManualCaseProgress.objects.update_or_create(
            test_run_test_case_id=pivot.pk,
            defaults={
                "claim_token": pivot.manual_claim_token,
                "current_step": current_step,
                "steps": [
                    step.to_wire() for step in sorted(steps, key=lambda s: s.num)
                ],
                "reported_at": timezone.now(),
            },
        )


def load_manual_progress(pivot: TestRunTestCase) -> ManualProgressView | None:
    """The runner's latest marks of the case's current claim (one query; none
    when the case is not claimed). None when nothing was reported under it."""
    if not pivot.manual_claim_token:
        return None
    row = (
        ManualCaseProgress.objects.filter(
            test_run_test_case_id=pivot.pk, claim_token=pivot.manual_claim_token
        )
        .only("current_step", "steps", "reported_at")
        .first()
    )
    if row is None:
        return None
    return ManualProgressView(
        current_step=row.current_step,
        steps=_parse_steps(row.steps),
        reported_at=row.reported_at,
    )


def _parse_steps(raw: object) -> tuple[ManualProgressStep, ...]:
    """The stored steps; malformed entries are skipped (the row is advisory)."""
    if not isinstance(raw, list):
        return ()
    parsed: list[ManualProgressStep] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        num = entry.get("num")
        outcome = entry.get("outcome")
        note = entry.get("note", "")
        if (
            isinstance(num, int)
            and not isinstance(num, bool)
            and isinstance(outcome, str)
            and outcome in _OUTCOMES
            and isinstance(note, str)
        ):
            parsed.append(ManualProgressStep(num=num, outcome=outcome, note=note))
    return tuple(parsed)
