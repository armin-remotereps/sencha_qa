"""Use case: a help thread's help panel state (draft, suggestion, fault, continue mode).

One payload builder for the three places that need it: the thread page (from
its already loaded ``HelpPanel``), the socket's ``current_state`` and the
``help`` broadcast after a help tool changed the request.
"""

from __future__ import annotations

import logging

from agents.plan_revision import PlanDraftView
from machine_setup.events import broadcast_help
from machine_setup.payloads import HelpThreadPayload, help_thread_payload
from machine_setup.services.case_help_context import help_request_is_waiting
from projects.case_help_services import HelpPanel
from projects.case_plan_revision_services import get_pending_plan_draft
from projects.models import CaseHelpRequest, HelpContinueMode, HelpProposalAction

logger = logging.getLogger(__name__)


def help_panel_payload(conversation_id: int, panel: HelpPanel) -> HelpThreadPayload:
    """The initial help state of a thread page, from its loaded panel (no query)."""
    waiting = panel.is_open
    return _payload(
        conversation_id,
        panel.request,
        waiting=waiting,
        draft=panel.pending_draft if waiting else None,
    )


def load_help_thread_payload(
    conversation_id: int, help_request_id: int
) -> HelpThreadPayload | None:
    """The help panel state of a help thread; two queries (one when resolved).

    None when the request row is gone.
    """
    request = (
        CaseHelpRequest.objects.select_related("test_run_test_case__test_run")
        .only(
            "id",
            "resolution",
            "agent_proposal_action",
            "agent_proposal_note",
            "agent_proposal_continue_mode",
            "fault",
            "continue_mode",
            "test_run_test_case__id",
            "test_run_test_case__status",
            "test_run_test_case__test_run__id",
            "test_run_test_case__test_run__status",
        )
        .filter(pk=help_request_id)
        .first()
    )
    if request is None:
        return None
    waiting = help_request_is_waiting(request)
    return _payload(
        conversation_id,
        request,
        waiting=waiting,
        draft=get_pending_plan_draft(request.pk) if waiting else None,
    )


def broadcast_help_thread(
    project_id: int, conversation_id: int, help_request_id: int
) -> None:
    """Push the thread's help panel; never raises (the page reloads it anyway)."""
    try:
        payload = load_help_thread_payload(conversation_id, help_request_id)
        if payload is not None:
            broadcast_help(project_id, payload)
    except Exception:
        logger.exception("Help thread %s: help broadcast failed", conversation_id)


def _payload(
    conversation_id: int,
    request: CaseHelpRequest,
    *,
    waiting: bool,
    draft: PlanDraftView | None,
) -> HelpThreadPayload:
    # The suggestion is shown only while the request waits for a person.
    action = request.agent_proposal_action if waiting else ""
    mode = request.agent_proposal_continue_mode if action else ""
    return help_thread_payload(
        conversation_id=conversation_id,
        request_open=waiting,
        resolution_label=request.get_resolution_display(),
        proposal_action=action,
        proposal_action_label=HelpProposalAction(action).label if action else "",
        proposal_note=request.agent_proposal_note if waiting else "",
        draft=draft,
        fault_label=request.get_fault_display() if request.fault else "",
        continue_mode=request.continue_mode,
        proposal_continue_mode=mode,
        proposal_continue_mode_label=HelpContinueMode(mode).label if mode else "",
    )
