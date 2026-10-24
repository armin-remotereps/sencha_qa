"""JSON payloads of chat rows and turns, shared by the page, socket and broadcasts.

Pure builders over already-loaded rows: callers fetch ``author`` and
``screenshots`` up front (select_related / Prefetch).
"""

from __future__ import annotations

import json
from typing import TypedDict

from django.conf import settings

from agents.plan_revision import PlanDraftView
from machine_setup.models import SetupMessage, SetupScreenshot, SetupTurn
from machine_setup.subtask_types import (
    DONE_WHEN_MAX_CHARS,
    GOAL_MAX_CHARS,
    KNOWN_STATE_MAX_CHARS,
    RUN_SETUP_TASK_TOOL,
)

_ARGUMENTS_TEXT_LIMIT = 4000
# Help panel caps (the services already cap these; the payload caps again).
_HELP_NOTE_LIMIT = 2000
_HELP_REASON_LIMIT = 1000
_HELP_STEP_TEXT_LIMIT = 2000
_HELP_ROWS_LIMIT = 60


class SetupScreenshotPayload(TypedDict):
    id: int
    url: str
    tool_name: str


class SetupMessagePayload(TypedDict):
    id: int
    conversation_id: int
    turn_id: int | None
    kind: str
    text: str
    author_name: str
    tool_name: str
    tool_arguments_text: str
    tool_status: str
    screenshots: list[SetupScreenshotPayload]
    link_url: str
    created_at: str
    # The run_setup_task row a sub-agent row belongs to; None at top level.
    parent_id: int | None
    subtask_result: str
    subtask_result_label: str
    # Sub-agent rows of a run_setup_task row left out of the page load.
    hidden_child_count: int


class SetupTurnPayload(TypedDict):
    id: int
    conversation_id: int
    status: str
    status_label: str
    ended_reason: str
    ended_reason_label: str
    error_message: str
    stop_requested: bool
    iterations: int
    max_iterations: int


def screenshot_payload(screenshot: SetupScreenshot) -> SetupScreenshotPayload:
    return {
        "id": screenshot.pk,
        "url": screenshot.image.url,
        "tool_name": screenshot.tool_name,
    }


def message_payload(
    message: SetupMessage,
    screenshots: list[SetupScreenshot] | None = None,
    *,
    hidden_child_count: int = 0,
) -> SetupMessagePayload:
    """``screenshots`` defaults to the prefetched ``message.screenshots``."""
    rows = screenshots if screenshots is not None else list(message.screenshots.all())
    author = message.author
    return {
        "id": message.pk,
        "conversation_id": message.conversation_id,
        "turn_id": message.turn_id,
        "kind": message.kind,
        "text": message.text,
        "author_name": (author.get_full_name() or author.email) if author else "",
        "tool_name": message.tool_name,
        "tool_arguments_text": (
            _delegation_arguments_text(message.tool_arguments)
            if message.tool_name == RUN_SETUP_TASK_TOOL
            else _arguments_text(message.tool_arguments)
        ),
        "tool_status": message.tool_status,
        "screenshots": [screenshot_payload(row) for row in rows],
        "link_url": message.link_url,
        "created_at": message.created_at.isoformat(),
        "parent_id": message.parent_id,
        "subtask_result": message.subtask_result,
        "subtask_result_label": (
            message.get_subtask_result_display() if message.subtask_result else ""
        ),
        "hidden_child_count": hidden_child_count,
    }


def turn_payload(turn: SetupTurn) -> SetupTurnPayload:
    return {
        "id": turn.pk,
        "conversation_id": turn.conversation_id,
        "status": turn.status,
        "status_label": turn.get_status_display(),
        "ended_reason": turn.ended_reason,
        "ended_reason_label": (
            turn.get_ended_reason_display() if turn.ended_reason else ""
        ),
        "error_message": turn.error_message,
        "stop_requested": turn.stop_requested,
        "iterations": turn.iterations,
        "max_iterations": settings.MACHINE_SETUP_MAX_ITERATIONS,
    }


# Per-field display caps of run_setup_task arguments (the tool's own limits).
_DELEGATION_FIELD_LIMITS = {
    "goal": GOAL_MAX_CHARS,
    "done_when": DONE_WHEN_MAX_CHARS,
    "known_state": KNOWN_STATE_MAX_CHARS,
}


def _delegation_arguments_text(arguments: object) -> str:
    """The page parses these to show the sub-task, so the JSON must stay whole.

    Each string field is cut to its own limit instead of cutting the JSON text.
    """
    if not isinstance(arguments, dict):
        return _arguments_text(arguments)
    capped = {
        key: (
            value[: _DELEGATION_FIELD_LIMITS[key]]
            if key in _DELEGATION_FIELD_LIMITS and isinstance(value, str)
            else value
        )
        for key, value in arguments.items()
        if key in _DELEGATION_FIELD_LIMITS or key in ("read_only", "timeout_seconds")
    }
    return json.dumps(capped, indent=2, ensure_ascii=False, default=str)


def _arguments_text(arguments: object) -> str:
    if not arguments:
        return ""
    text = json.dumps(arguments, indent=2, ensure_ascii=False, default=str)
    if len(text) > _ARGUMENTS_TEXT_LIMIT:
        return text[:_ARGUMENTS_TEXT_LIMIT] + "\n…"
    return text


class HelpProposalPayload(TypedDict):
    action: str
    action_label: str
    note: str


class PlanDraftOldRowPayload(TypedDict):
    num: int
    description: str
    status: str
    status_label: str
    # New step it is carried to; None when dropped.
    carried_to: int | None


class PlanDraftNewRowPayload(TypedDict):
    num: int
    description: str
    expected_result: str
    carried_from: int | None
    # "carried", "resume" or "run".
    state: str
    state_label: str


class PlanDraftPayload(TypedDict):
    revision_id: int
    reason: str
    resume_step: int
    carried_count: int
    old_rows: list[PlanDraftOldRowPayload]
    new_rows: list[PlanDraftNewRowPayload]


class HelpThreadPayload(TypedDict):
    """The help panel of a help thread (auto-fix or help chat)."""

    conversation_id: int
    # The request still waits for a person (open, run paused, case Need help).
    request_open: bool
    resolution_label: str
    agent_proposal: HelpProposalPayload | None
    plan_draft: PlanDraftPayload | None


def help_thread_payload(
    *,
    conversation_id: int,
    request_open: bool,
    resolution_label: str,
    proposal_action: str,
    proposal_action_label: str,
    proposal_note: str,
    draft: PlanDraftView | None,
) -> HelpThreadPayload:
    return {
        "conversation_id": conversation_id,
        "request_open": request_open,
        "resolution_label": resolution_label,
        "agent_proposal": (
            {
                "action": proposal_action,
                "action_label": proposal_action_label,
                "note": proposal_note[:_HELP_NOTE_LIMIT],
            }
            if proposal_action
            else None
        ),
        "plan_draft": plan_draft_payload(draft) if draft is not None else None,
    }


def plan_draft_payload(draft: PlanDraftView) -> PlanDraftPayload:
    return {
        "revision_id": draft.revision_id,
        "reason": draft.reason[:_HELP_REASON_LIMIT],
        "resume_step": draft.resume_step,
        "carried_count": draft.carried_count,
        "old_rows": [
            {
                "num": row.num,
                "description": row.description[:_HELP_STEP_TEXT_LIMIT],
                "status": row.status,
                "status_label": row.status_label,
                "carried_to": row.carried_to,
            }
            for row in draft.old_rows[:_HELP_ROWS_LIMIT]
        ],
        "new_rows": [
            {
                "num": row.num,
                "description": row.description[:_HELP_STEP_TEXT_LIMIT],
                "expected_result": row.expected_result[:_HELP_STEP_TEXT_LIMIT],
                "carried_from": row.carried_from,
                "state": row.state,
                "state_label": row.state_label,
            }
            for row in draft.new_rows[:_HELP_ROWS_LIMIT]
        ],
    }
