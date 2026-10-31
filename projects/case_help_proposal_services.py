"""The help-chat agent's suggested resolution of a Need help case.

The agent only suggests (Continue / Mark failed / Skip, with a note, and for
Continue optionally a continue mode); the suggestion is shown next to the
person's form, never copied into it or into the help key's ``note`` (that
note reaches the tester as the person's own words), and only the person acts.
Every resolution clears it (``clear_agent_proposal``).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from projects.case_pause_policy import allowed_continue_modes
from projects.case_plan_revision_services import lock_waiting_help_request
from projects.exceptions import RunHelpError
from projects.models import (
    HELP_NOTE_MAX_LENGTH,
    CaseHelpRequest,
    HelpContinueMode,
    HelpProposalAction,
)

_PROPOSAL_FIELDS = [
    "agent_proposal_action",
    "agent_proposal_note",
    "agent_proposal_at",
    "agent_proposal_continue_mode",
]
_MODE_NEEDS_CONTINUE = "A continue mode goes only with the continue action."


def record_agent_proposal(
    *,
    help_request_id: int,
    action: HelpProposalAction,
    note: str,
    continue_mode: HelpContinueMode | None = None,
) -> None:
    """Store the agent's suggestion on the open request.

    ``continue_mode`` (Continue only) must be one the request allows
    (``allowed_continue_modes``, checked on the locked case's checkpoint).

    Raises ``RunHelpError("This case is no longer waiting for help.")`` unless
    the run is PAUSED, the request open and the case Need help, and a
    ``RunHelpError`` for a mode with another action or a mode not allowed.
    """
    with transaction.atomic():
        _, pivot, request = lock_waiting_help_request(help_request_id)
        if continue_mode is not None:
            if action != HelpProposalAction.CONTINUE:
                raise RunHelpError(_MODE_NEEDS_CONTINUE)
            allowed = allowed_continue_modes(
                request.continue_mode, request.fault, pivot.execution_checkpoint
            )
            if continue_mode not in allowed:
                names = ", ".join(str(mode.value) for mode in allowed)
                raise RunHelpError(f"That mode is not allowed for this case: {names}")
        request.agent_proposal_action = action
        request.agent_proposal_note = note.strip()[:HELP_NOTE_MAX_LENGTH]
        request.agent_proposal_at = timezone.now()
        request.agent_proposal_continue_mode = (
            continue_mode.value if continue_mode is not None else ""
        )
        request.save(update_fields=_PROPOSAL_FIELDS)


def clear_agent_proposal(request: CaseHelpRequest) -> None:
    """Forget the agent's suggestion (the request is being resolved); no-op if none."""
    if (
        not request.agent_proposal_action
        and not request.agent_proposal_note
        and request.agent_proposal_at is None
        and not request.agent_proposal_continue_mode
    ):
        return
    request.agent_proposal_action = HelpProposalAction.NONE
    request.agent_proposal_note = ""
    request.agent_proposal_at = None
    request.agent_proposal_continue_mode = ""
    request.save(update_fields=_PROPOSAL_FIELDS)
