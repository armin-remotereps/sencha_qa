"""The help-chat agent's suggested resolution of a Need help case.

The agent only suggests (Continue / Mark failed / Skip, with a note); the
suggestion pre-selects and pre-fills the person's form, and only the person
acts. Every resolution clears it (``clear_agent_proposal``).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from projects.case_plan_revision_services import lock_waiting_help_request
from projects.models import HELP_NOTE_MAX_LENGTH, CaseHelpRequest, HelpProposalAction

_PROPOSAL_FIELDS = ["agent_proposal_action", "agent_proposal_note", "agent_proposal_at"]


def record_agent_proposal(
    *, help_request_id: int, action: HelpProposalAction, note: str
) -> None:
    """Store the agent's suggestion on the open request.

    Raises ``RunHelpError("This case is no longer waiting for help.")`` unless
    the run is PAUSED, the request open and the case Need help.
    """
    with transaction.atomic():
        _, _, request = lock_waiting_help_request(help_request_id)
        request.agent_proposal_action = action
        request.agent_proposal_note = note.strip()[:HELP_NOTE_MAX_LENGTH]
        request.agent_proposal_at = timezone.now()
        request.save(update_fields=_PROPOSAL_FIELDS)


def clear_agent_proposal(request: CaseHelpRequest) -> None:
    """Forget the agent's suggestion (the request is being resolved); no-op if none."""
    if (
        not request.agent_proposal_action
        and not request.agent_proposal_note
        and request.agent_proposal_at is None
    ):
        return
    request.agent_proposal_action = HelpProposalAction.NONE
    request.agent_proposal_note = ""
    request.agent_proposal_at = None
    request.save(update_fields=_PROPOSAL_FIELDS)
