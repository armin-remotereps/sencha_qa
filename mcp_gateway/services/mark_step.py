"""Use case: the MCP tester marks one step of its claimed case (mark_step).

Under the case row lock: the step must be one of the claim's steps; Pass and
Fail both need 1..``MANUAL_STEP_MAX_SHOTS`` of the claim's own staged shots
(the hand-in names each step's attempt after its first screenshot, as the
window does), Fail also a note. The shots are bound to the step (kind pass
or fail); re-marking a step unbinds its earlier shots. The step's time is
the time since the previous mark (or the claim), added to the step's earlier
time on a re-mark, capped at ``MANUAL_STEP_MAX_SECONDS``. All marks are then
written to the advisory progress row (``record_manual_progress``) for the
web card. Returns every mark so far.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from controller_client.protocol import (
    MANUAL_STEP_MAX_SECONDS,
    MANUAL_STEP_MAX_SHOTS,
    ManualProgressStepPayload,
    ManualShotKind,
    ManualStepOutcome,
)
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.models import McpStagedShot, McpStepMark
from mcp_gateway.schemas import MarkStepRequest, MarkStepResponse
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.own_claim import (
    claim_marks,
    load_own_claim,
    refuse_paused,
)
from projects.manual_case_progress_services import record_manual_progress

_NO_STEP: Final = "This case has no step {num}; it has {count}."
_FAIL_NEEDS_NOTE: Final = "A failed step needs a note saying what went wrong."
_SHOT_COUNT: Final = (
    f"Mark a step with 1 to {MANUAL_STEP_MAX_SHOTS} screenshots (shot_id values "
    "from add_screenshot)."
)
_DUPLICATE_SHOT: Final = "shot_ids lists the same screenshot twice."
_UNKNOWN_SHOT: Final = (
    "A shot_id is not a screenshot of this claim. Send it with add_screenshot " "first."
)
_SHOT_ON_OTHER_STEP: Final = (
    "A screenshot is already used for step {num}. Each screenshot belongs to "
    "one step; send a new one."
)


def mark_step(call: McpCall, request: MarkStepRequest) -> MarkStepResponse:
    outcome = ManualStepOutcome(request.outcome)
    note = request.note.strip()
    if outcome is ManualStepOutcome.FAIL and not note:
        raise McpToolError(_FAIL_NEEDS_NOTE)
    shot_ids = request.shot_ids
    if not 1 <= len(shot_ids) <= MANUAL_STEP_MAX_SHOTS:
        raise McpToolError(_SHOT_COUNT)
    if len(set(shot_ids)) != len(shot_ids):
        raise McpToolError(_DUPLICATE_SHOT)
    with transaction.atomic():
        claim = load_own_claim(
            project_id=call.project.pk,
            pivot_id=request.pivot_id,
            claim_token=request.claim_token,
            lock=True,
        )
        refuse_paused(claim)
        if request.step_num > claim.step_count:
            raise McpToolError(
                _NO_STEP.format(num=request.step_num, count=claim.step_count)
            )
        claim_shots = McpStagedShot.objects.filter(
            pivot_id=request.pivot_id, claim_token=request.claim_token
        )
        found = dict(
            claim_shots.filter(shot_key__in=shot_ids).values_list(
                "shot_key", "step_num"
            )
        )
        if len(found) != len(shot_ids):
            raise McpToolError(_UNKNOWN_SHOT)
        for bound_step in found.values():
            if bound_step is not None and bound_step != request.step_num:
                raise McpToolError(_SHOT_ON_OTHER_STEP.format(num=bound_step))

        now = timezone.now()
        claim_shots.filter(step_num=request.step_num).exclude(
            shot_key__in=shot_ids
        ).update(step_num=None, kind="", bound_at=None)
        kind = (
            ManualShotKind.PASS
            if outcome is ManualStepOutcome.PASS
            else ManualShotKind.FAIL
        )
        claim_shots.filter(shot_key__in=shot_ids).update(
            step_num=request.step_num, kind=kind.value, bound_at=now
        )

        marks = McpStepMark.objects.filter(
            pivot_id=request.pivot_id, claim_token=request.claim_token
        )
        last_marked = marks.aggregate(last=Max("marked_at"))["last"]
        previous = marks.filter(step_num=request.step_num).first()
        seconds = _step_seconds(
            now=now,
            since=last_marked or claim.pivot.manual_claimed_at,
            earlier=previous.seconds if previous is not None else 0,
        )
        McpStepMark.objects.update_or_create(
            pivot_id=request.pivot_id,
            claim_token=request.claim_token,
            step_num=request.step_num,
            defaults={
                "outcome": outcome.value,
                "note": note,
                "seconds": seconds,
                "marked_at": now,
            },
        )
        all_marks = list(
            marks.order_by("step_num").values_list("step_num", "outcome", "note")
        )
        record_manual_progress(
            project=call.project,
            pivot_id=request.pivot_id,
            claim_token=request.claim_token,
            current_step=min(request.step_num + 1, claim.step_count),
            steps=[
                ManualProgressStepPayload(
                    num=num, outcome=ManualStepOutcome(step_outcome), note=step_note
                )
                for num, step_outcome, step_note in all_marks
            ],
        )
        result = claim_marks(request.pivot_id, request.claim_token)
    return MarkStepResponse(marks=result, step_count=claim.step_count)


def _step_seconds(*, now: datetime, since: datetime | None, earlier: int) -> int:
    elapsed = int((now - since).total_seconds()) if since is not None else 0
    return min(max(earlier, 0) + max(elapsed, 0), MANUAL_STEP_MAX_SECONDS)
