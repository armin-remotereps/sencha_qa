"""Live updates of a run proposal's card in the main chat."""

from __future__ import annotations

from machine_setup.events import broadcast_message
from machine_setup.models import SetupMessage, SetupMessageKind
from machine_setup.payloads import message_payload


def broadcast_proposal_card(*, proposal_id: int) -> None:
    """Re-broadcast the NOTICE row(s) that show the proposal's card.

    One query; ``case_ids`` is never loaded.
    """
    rows = (
        SetupMessage.objects.filter(
            run_proposal_id=proposal_id, kind=SetupMessageKind.NOTICE
        )
        .select_related("author", "run_proposal")
        .defer("run_proposal__case_ids")
    )
    for row in rows:
        proposal = row.run_proposal
        if proposal is None:
            continue
        broadcast_message(
            proposal.project_id, message_payload(row, screenshots=[], attachments=[])
        )
