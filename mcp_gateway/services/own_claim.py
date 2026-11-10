"""The MCP tester's own claim: its checks, its staged rows, its steps.

Shared by the write use cases (``add_screenshot``, ``mark_step``,
``hand_in``, ``release_case``) and ``read_case``. The gateway acts only on
claims it made itself (``manual_claim_source == MANUAL_SOURCE_MCP``); a claim
of the Test Runner window is never reachable with its token.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Final

from agents.checkpoint import checkpoint_from_json
from agents.types import OrchestratorCheckpoint
from controller_client.protocol import MANUAL_STEP_TEXT_MAX_CHARS
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.models import McpStagedShot, McpStepMark
from mcp_gateway.schemas import MarkItem, StepItem
from mcp_gateway.untrusted_text import fence_case_text
from projects.case_markup import markup_to_plain
from projects.manual_case_lock import claim_matches
from projects.manual_case_steps import ManualStep, manual_steps_from_checkpoint
from projects.manual_run_policy import is_claimed_manual_case
from projects.models import MANUAL_SOURCE_MCP, TestRun, TestRunTestCase

NOT_YOUR_CLAIM: Final = (
    "This case is not claimed by you any more (released, handed in, re-run or "
    "the run was stopped). Call get_case to see its state."
)
NO_STEPS: Final = "This case has no manual steps."
PAUSED: Final = "This case is paused; wait until it is continued."


@dataclass(frozen=True)
class OwnClaim:
    """A locked (or freshly read) case held by an MCP claim."""

    pivot: TestRunTestCase
    checkpoint: OrchestratorCheckpoint

    @property
    def step_count(self) -> int:
        return len(self.checkpoint.plan)


def load_own_claim(
    *, project_id: int, pivot_id: int, claim_token: str, lock: bool
) -> OwnClaim:
    """The case if it is the project's running Manual run case held by the
    MCP claim ``claim_token`` (one query; ``lock`` takes the case row lock,
    inside the caller's transaction). Raises ``McpToolError``."""
    cases = TestRunTestCase.objects.select_related("test_run")
    if lock:
        cases = cases.select_for_update(of=("self",))
    pivot = (
        cases.only(
            "id",
            "status",
            "manual_claim_token",
            "manual_claim_source",
            "manual_claimed_at",
            "manual_paused_at",
            "execution_checkpoint",
            "test_run__id",
            "test_run__status",
            "test_run__help_mode",
            # The run's test machine (the MCP machine tools' runner).
            "test_run__runner_id",
        )
        .filter(pk=pivot_id, test_run__project_id=project_id)
        .first()
    )
    if pivot is None:
        raise McpToolError(NOT_YOUR_CLAIM)
    return check_own_claim(pivot.test_run, pivot, claim_token)


def check_own_claim(
    test_run: TestRun, pivot: TestRunTestCase, claim_token: str
) -> OwnClaim:
    """``pivot`` (of ``test_run``, already loaded) as the MCP claim
    ``claim_token``; no query. Raises ``McpToolError``."""
    if (
        pivot.manual_claim_source != MANUAL_SOURCE_MCP
        or not is_claimed_manual_case(test_run, pivot)
        or not claim_matches(pivot, claim_token)
    ):
        raise McpToolError(NOT_YOUR_CLAIM)
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    if checkpoint is None or not checkpoint.manual or not checkpoint.plan:
        raise McpToolError(NO_STEPS)
    return OwnClaim(pivot=pivot, checkpoint=checkpoint)


def refuse_paused(claim: OwnClaim) -> None:
    """Refuse a write while a member has paused the claim on the web
    (retryable; release stays allowed)."""
    if claim.pivot.manual_paused_at is not None:
        raise McpToolError(PAUSED)


def step_items(checkpoint: OrchestratorCheckpoint) -> list[StepItem]:
    """The claim's verbatim steps as the client gets them (fenced)."""
    return steps_to_items(manual_steps_from_checkpoint(checkpoint))


def steps_to_items(steps: tuple[ManualStep, ...]) -> list[StepItem]:
    return [
        StepItem(
            num=step.num,
            text=fence_case_text(
                markup_to_plain(step.text)[:MANUAL_STEP_TEXT_MAX_CHARS]
            ),
            expected=fence_case_text(
                markup_to_plain(step.expected)[:MANUAL_STEP_TEXT_MAX_CHARS]
            ),
        )
        for step in steps
    ]


def claim_marks(pivot_id: int, claim_token: str) -> list[MarkItem]:
    """The claim's marks with their bound shots, by step (two queries)."""
    marks = list(
        McpStepMark.objects.filter(pivot_id=pivot_id, claim_token=claim_token)
        .order_by("step_num")
        .values_list("step_num", "outcome", "note")
    )
    if not marks:
        return []
    shots = bound_shot_keys(pivot_id, claim_token)
    return [
        MarkItem(
            num=num,
            outcome=outcome,
            note=note,
            shot_ids=shots.get(num, []),
        )
        for num, outcome, note in marks
    ]


def bound_shot_keys(pivot_id: int, claim_token: str) -> dict[int, list[str]]:
    """The claim's step-bound shot keys by step, in staging order (one query)."""
    by_step: defaultdict[int, list[str]] = defaultdict(list)
    for step_num, key in (
        McpStagedShot.objects.filter(
            pivot_id=pivot_id, claim_token=claim_token, step_num__isnull=False
        )
        .order_by("created_at", "pk")
        .values_list("step_num", "shot_key")
    ):
        if step_num is not None:
            by_step[step_num].append(key)
    return dict(by_step)


def unbound_shot_keys(pivot_id: int, claim_token: str) -> list[str]:
    return list(
        McpStagedShot.objects.filter(
            pivot_id=pivot_id, claim_token=claim_token, step_num__isnull=True
        )
        .order_by("created_at", "pk")
        .values_list("shot_key", flat=True)
    )


def delete_claim_staging(pivot_id: int, claim_token: str) -> None:
    """Drop every staged shot and mark of one claim (two queries)."""
    McpStagedShot.objects.filter(pivot_id=pivot_id, claim_token=claim_token).delete()
    McpStepMark.objects.filter(pivot_id=pivot_id, claim_token=claim_token).delete()
