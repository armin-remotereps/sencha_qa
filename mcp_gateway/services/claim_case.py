"""Use case: the MCP tester claims a Manual run case (claim_case).

``claim_manual_case`` with the neutral tester and the internal MCP source,
then the claimed steps as the checkpoint stored them. Every claim rule (run
running, case Created, one claim per run, no live setup turn) stays there.
"""

from __future__ import annotations

from controller_client.protocol import (
    MANUAL_PRECONDITIONS_MAX_CHARS,
    MANUAL_STEP_MAX_SHOTS,
)
from mcp_gateway.schemas import CaseRequest, ClaimResponse
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.own_claim import load_own_claim, step_items
from mcp_gateway.untrusted_text import case_line, fence_case_text
from projects.case_markup import markup_to_plain
from projects.manual_case_claim_services import claim_manual_case
from projects.models import MANUAL_SOURCE_MCP, TestCase


def claim_case(call: McpCall, request: CaseRequest) -> ClaimResponse:
    token = claim_manual_case(
        project=call.project,
        pivot_id=request.pivot_id,
        actor=call.actor,
        source=MANUAL_SOURCE_MCP,
    )
    claim = load_own_claim(
        project_id=call.project.pk,
        pivot_id=request.pivot_id,
        claim_token=token,
        lock=False,
    )
    test_case = (
        TestCase.objects.filter(run_entries__pk=request.pivot_id)
        .values_list("title", "preconditions")
        .get()
    )
    return ClaimResponse(
        pivot_id=request.pivot_id,
        claim_token=token,
        title=case_line(test_case[0]),
        preconditions=fence_case_text(
            markup_to_plain(test_case[1])[:MANUAL_PRECONDITIONS_MAX_CHARS]
        ),
        steps=step_items(claim.checkpoint),
        step_max_shots=MANUAL_STEP_MAX_SHOTS,
    )
