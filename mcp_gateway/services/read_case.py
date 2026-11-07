"""Use case: the MCP tester reads one Manual run case (get_case).

Title, TestRail id, preconditions and verbatim steps (from the claim's
checkpoint once claimed or handed in, else built from the test case, as the
claim would), the status and, after a hand-in, the verdict. For the client's
own MCP claim also the claim token, the marks so far and the staged shots.
At most four queries.
"""

from __future__ import annotations

from typing import Final

from agents.checkpoint import checkpoint_from_json
from controller_client.protocol import MANUAL_PRECONDITIONS_MAX_CHARS
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.schemas import CaseRequest, CaseResponse, MyClaim
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.own_claim import (
    claim_marks,
    steps_to_items,
    unbound_shot_keys,
)
from mcp_gateway.untrusted_text import case_line, fence_case_text
from projects.case_markup import markup_to_plain
from projects.manual_case_steps import manual_case_steps, manual_steps_from_checkpoint
from projects.models import (
    FINISHED_CASE_STATUSES,
    MANUAL_SOURCE_MCP,
    ManualCaseSubmission,
    ManualUploadState,
    ManualVerdict,
    RunHelpMode,
    TestRunTestCase,
)

_NO_CASE: Final = "This project has no Manual run case with that id."
WIRE_VERDICTS: Final[dict[str, str]] = {
    ManualVerdict.SUCCESS: "passed",
    ManualVerdict.FAILED: "failed",
    ManualVerdict.BLOCKED: "blocked",
}


def read_case(call: McpCall, request: CaseRequest) -> CaseResponse:
    pivot = (
        # The whole test case: the steps parser reads several of its fields.
        TestRunTestCase.objects.select_related("test_case")
        .defer("logs")
        .filter(
            pk=request.pivot_id,
            test_run__project_id=call.project.pk,
            test_run__help_mode=RunHelpMode.MANUAL,
        )
        .first()
    )
    if pivot is None:
        raise McpToolError(_NO_CASE)
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    if checkpoint is not None and checkpoint.manual and checkpoint.plan:
        steps = manual_steps_from_checkpoint(checkpoint)
    else:
        steps = manual_case_steps(pivot.test_case).steps

    verdict: str | None = None
    verdict_note: str | None = None
    if pivot.status in FINISHED_CASE_STATUSES:
        # The current hand-in (as ``projects.manual_current_submission``).
        submission = (
            ManualCaseSubmission.objects.filter(test_run_test_case_id=pivot.pk)
            .exclude(upload_state=ManualUploadState.SUPERSEDED)
            .order_by("-submitted_at", "-id")
            .values_list("verdict", "verdict_note")
            .first()
        )
        if submission is not None:
            verdict = WIRE_VERDICTS.get(submission[0])
            verdict_note = fence_case_text(submission[1])

    token = pivot.manual_claim_token
    my_claim = None
    if token and pivot.manual_claim_source == MANUAL_SOURCE_MCP:
        my_claim = MyClaim(
            claim_token=token,
            marks=claim_marks(pivot.pk, token),
            unbound_shot_ids=unbound_shot_keys(pivot.pk, token),
        )
    return CaseResponse(
        pivot_id=pivot.pk,
        run_id=pivot.test_run_id,
        title=case_line(pivot.test_case.title),
        testrail_id=case_line(pivot.test_case.testrail_id or ""),
        preconditions=fence_case_text(
            markup_to_plain(pivot.test_case.preconditions)[
                :MANUAL_PRECONDITIONS_MAX_CHARS
            ]
        ),
        steps=steps_to_items(steps),
        status=pivot.status,
        verdict=verdict,
        verdict_note=verdict_note,
        claimed=bool(token),
        my_claim=my_claim,
    )
