"""Use case: the MCP tester gives its claim back (release_case).

Only the client's own MCP claim, with its token; ``release_manual_case``
puts the case back to Created. The claim's staged screenshots and marks are
deleted in the same transaction.
"""

from __future__ import annotations

from typing import Final

from django.db import transaction

from mcp_gateway.schemas import ClaimRequest, MessageResponse
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.own_claim import delete_claim_staging, load_own_claim
from mcp_gateway.tasks import stop_claim_commands
from projects.manual_case_claim_services import release_manual_case

_RELEASED: Final = "Released. The case is back in the list."


def release_case(call: McpCall, request: ClaimRequest) -> MessageResponse:
    # Own MCP claim first (the token alone could not tell a window claim apart).
    load_own_claim(
        project_id=call.project.pk,
        pivot_id=request.pivot_id,
        claim_token=request.claim_token,
        lock=False,
    )
    with transaction.atomic():
        release_manual_case(
            project=call.project,
            pivot_id=request.pivot_id,
            actor=call.actor,
            claim_token=request.claim_token,
        )
        delete_claim_staging(request.pivot_id, request.claim_token)
        project_id, pivot_id = call.project.pk, request.pivot_id
        # After the commit, stop the claim's kept commands (D7, best effort;
        # ``robust``: a broker fault never fails the release, the minute
        # sweep catches up).
        transaction.on_commit(
            lambda: stop_claim_commands.delay(project_id, pivot_id), robust=True
        )
    return MessageResponse(message=_RELEASED)
