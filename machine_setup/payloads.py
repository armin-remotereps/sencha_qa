"""JSON payloads of chat rows and turns, shared by the page, socket and broadcasts.

Pure builders over already-loaded rows: callers fetch ``author`` and
``screenshots`` up front (select_related / Prefetch).
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict

from django.conf import settings
from django.db.models import Prefetch, QuerySet
from django.urls import reverse

from agents.plan_revision import PlanDraftView
from knowledge_base.models import KnowledgeItem
from knowledge_base.types import KnowledgeItemSummary
from machine_setup.models import (
    SetupDocument,
    SetupMessage,
    SetupMessageKind,
    SetupRunProposal,
    SetupScreenshot,
    SetupTurn,
)
from machine_setup.run_proposal_types import (
    PROPOSAL_NOTE_MAX,
    RUN_NAME_MAX,
    SAMPLE_TITLES,
)
from machine_setup.subtask_types import (
    DONE_WHEN_MAX_CHARS,
    GOAL_MAX_CHARS,
    KNOWN_STATE_MAX_CHARS,
    RUN_SETUP_TASK_TOOL,
)
from projects.case_result_change_services import get_result_change_view
from projects.case_review_redesign_services import get_review_draft_view
from projects.models import CasePlanRevision, CaseResultChange

_ARGUMENTS_TEXT_LIMIT = 4000
# Help panel caps (the services already cap these; the payload caps again).
_HELP_NOTE_LIMIT = 2000
_HELP_REASON_LIMIT = 1000
_HELP_STEP_TEXT_LIMIT = 2000
_HELP_ROWS_LIMIT = 60
_SAMPLE_TITLE_LIMIT = 200
_PROPOSAL_ROWS_LIMIT = 50
# Result-chat card caps (the services already cap these; the payload caps again).
_RESULT_SUMMARY_LIMIT = 4000
_RESULT_EXPLANATION_LIMIT = 2000
_RESULT_EFFECTS_LIMIT = 1000
_CONFIRM_BODY_LIMIT = 1000

# What a SetupMessage load joins so the result-chat cards build without
# further queries (``message_payload`` reads both FKs when set; the caller
# passes ``project_id``, so the run is not joined). The case's log is never
# read; a result change's card reads only the case's result fields, so its
# checkpoint is deferred too. A redesign's card needs the checkpoint for the
# plan fingerprint (there is no stored one to compare instead).
CARD_SELECT_RELATED: tuple[str, ...] = (
    "result_change__test_run_test_case",
    "result_change__decided_by",
    "plan_revision__test_run_test_case",
)
CARD_DEFER: tuple[str, ...] = (
    "result_change__test_run_test_case__logs",
    "result_change__test_run_test_case__execution_checkpoint",
    "plan_revision__test_run_test_case__logs",
)


# The KnowledgeItem columns a reference chip reads; never the description.
KNOWLEDGE_REF_FIELDS: tuple[str, ...] = ("id", "project_id", "name", "status")


def knowledge_refs_prefetch() -> (
    Prefetch[Literal["knowledge_items"], QuerySet[KnowledgeItem], str]
):
    """Prefetch of ``SetupMessage.knowledge_items`` for ``message_payload``."""
    return Prefetch(
        "knowledge_items",
        queryset=KnowledgeItem.objects.only(*KNOWLEDGE_REF_FIELDS).order_by("id"),
    )


class SetupScreenshotPayload(TypedDict):
    id: int
    url: str
    tool_name: str


class SetupAttachmentPayload(TypedDict):
    id: int
    file_name: str
    size_bytes: int
    char_count: int


class KnowledgeRefPayload(TypedDict):
    """A knowledge base item a USER message referenced (composer picker)."""

    id: int
    name: str
    # pending | extracting | ready | failed
    status: str
    # The item's knowledge base page.
    url: str


class KnowledgeChoicePayload(TypedDict):
    """One entry of the composer's knowledge base picker."""

    id: int
    name: str
    kind_label: str
    status: str
    # An archive's member file.
    in_archive: bool
    # A member's folder inside its archive, "" at the root or for uploads.
    folder: str
    # Archives: how many member files.
    member_count: int


class ProposedRunPayload(TypedDict):
    name: str
    case_count: int
    sample_titles: list[str]
    # A run of this project already has this name.
    name_exists: bool


class CreatedRunPayload(TypedDict):
    name: str
    case_count: int
    url: str


class RunProposalPayload(TypedDict):
    id: int
    status: str
    status_label: str
    note: str
    runs: list[ProposedRunPayload]
    created_runs: list[CreatedRunPayload]
    skipped: list[str]


class ResultChangeCardPayload(TypedDict):
    change_id: int
    # A ``projects.models.ResultChangeStatus`` value, and its label.
    status: str
    status_label: str
    from_label: str
    to_label: str
    summary: str
    explanation: str
    effects: str
    # The case changed since the proposal: only Discard is offered.
    outdated: bool
    decided_by_name: str
    apply_url: str
    discard_url: str


class PlanRedesignCardPayload(TypedDict):
    revision_id: int
    # A ``projects.models.PlanRevisionStatus`` value, and its label.
    status: str
    status_label: str
    reason: str
    resume_step: int
    carried_count: int
    old_rows: list["PlanDraftOldRowPayload"]
    new_rows: list["PlanDraftNewRowPayload"]
    outdated: bool
    # Server-built from counts only (no model text).
    confirm_body: str
    rerun_url: str
    discard_url: str


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
    # USER rows only: documents attached to the message.
    attachments: list[SetupAttachmentPayload]
    # USER rows only: knowledge base items the message referenced.
    knowledge_refs: list[KnowledgeRefPayload]
    # NOTICE rows that show a run proposal card.
    run_proposal: RunProposalPayload | None
    # NOTICE rows of a result chat that show a result change / redesign card.
    result_change: ResultChangeCardPayload | None
    plan_redesign: PlanRedesignCardPayload | None


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


def knowledge_ref_payload(
    *, project_id: int, item_id: int, name: str, status: str
) -> KnowledgeRefPayload:
    return {
        "id": item_id,
        "name": name,
        "status": status,
        "url": reverse(
            "knowledge_base:item",
            kwargs={"project_id": project_id, "item_id": item_id},
        ),
    }


def knowledge_choice_payload(summary: KnowledgeItemSummary) -> KnowledgeChoicePayload:
    return {
        "id": summary.id,
        "name": summary.name,
        "kind_label": summary.kind_label,
        "status": summary.status,
        "in_archive": summary.parent_id is not None,
        "folder": summary.folder,
        "member_count": summary.member_count,
    }


def knowledge_item_ref_payload(item: KnowledgeItem) -> KnowledgeRefPayload:
    """From a row loaded with ``KNOWLEDGE_REF_FIELDS``."""
    return knowledge_ref_payload(
        project_id=item.project_id, item_id=item.pk, name=item.name, status=item.status
    )


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
    attachments: list[SetupDocument] | None = None,
    project_id: int | None = None,
    knowledge_refs: list[KnowledgeRefPayload] | None = None,
) -> SetupMessagePayload:
    """``screenshots`` defaults to the prefetched ``message.screenshots``.

    ``attachments`` defaults to the prefetched ``message.documents``, read for
    USER rows only, and ``knowledge_refs`` to the prefetched
    ``message.knowledge_items`` (``knowledge_refs_prefetch()``), USER rows only;
    the proposal is read only when ``run_proposal_id`` is set
    (load it with ``select_related`` and ``case_ids`` deferred), and the
    result-chat cards only when ``result_change_id`` / ``plan_revision_id``
    is set (load them with ``CARD_SELECT_RELATED`` / ``CARD_DEFER`` and pass
    ``project_id``; without it a card loads its run, one query). These rules
    keep tool and child rows free of extra queries.
    """
    rows = screenshots if screenshots is not None else list(message.screenshots.all())
    if attachments is None:
        attachments = (
            list(message.documents.all())
            if message.kind == SetupMessageKind.USER
            else []
        )
    if knowledge_refs is None:
        knowledge_refs = (
            [knowledge_item_ref_payload(item) for item in message.knowledge_items.all()]
            if message.kind == SetupMessageKind.USER
            else []
        )
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
        "attachments": [attachment_payload(row) for row in attachments],
        "knowledge_refs": knowledge_refs,
        "run_proposal": (
            run_proposal_payload(message.run_proposal)
            if message.run_proposal_id is not None and message.run_proposal
            else None
        ),
        "result_change": (
            result_change_card_payload(message.result_change, project_id)
            if message.result_change_id is not None and message.result_change
            else None
        ),
        "plan_redesign": (
            plan_redesign_card_payload(message.plan_revision, project_id)
            if message.plan_revision_id is not None and message.plan_revision
            else None
        ),
    }


def result_change_card_payload(
    change: CaseResultChange, project_id: int | None = None
) -> ResultChangeCardPayload:
    """Reads the change's case (``CARD_SELECT_RELATED``); the run only when
    ``project_id`` is not given."""
    pivot = change.test_run_test_case
    view = get_result_change_view(change, pivot)
    if project_id is None:
        project_id = pivot.test_run.project_id
    return {
        "change_id": view.change_id,
        "status": view.status,
        "status_label": view.status_label,
        "from_label": view.from_label,
        "to_label": view.to_label,
        "summary": view.summary[:_RESULT_SUMMARY_LIMIT],
        "explanation": view.explanation[:_RESULT_EXPLANATION_LIMIT],
        "effects": view.effects[:_RESULT_EFFECTS_LIMIT],
        "outdated": view.outdated,
        "decided_by_name": view.decided_by_name,
        "apply_url": reverse(
            "machine_setup:result_change_apply",
            kwargs={"project_id": project_id, "change_id": change.pk},
        ),
        "discard_url": reverse(
            "machine_setup:result_change_discard",
            kwargs={"project_id": project_id, "change_id": change.pk},
        ),
    }


def plan_redesign_card_payload(
    revision: CasePlanRevision, project_id: int | None = None
) -> PlanRedesignCardPayload:
    """Reads the revision's case (``CARD_SELECT_RELATED``); the run only when
    ``project_id`` is not given."""
    pivot = revision.test_run_test_case
    view = get_review_draft_view(revision, pivot)
    rows = plan_draft_payload(view.draft)
    if project_id is None:
        project_id = pivot.test_run.project_id
    return {
        "revision_id": view.revision_id,
        "status": view.status,
        "status_label": revision.get_status_display(),
        "reason": view.reason[:_HELP_REASON_LIMIT],
        "resume_step": view.resume_step,
        "carried_count": view.draft.carried_count,
        "old_rows": rows["old_rows"],
        "new_rows": rows["new_rows"],
        "outdated": view.outdated,
        "confirm_body": view.confirm_body[:_CONFIRM_BODY_LIMIT],
        "rerun_url": reverse(
            "machine_setup:plan_redesign_rerun",
            kwargs={"project_id": project_id, "revision_id": revision.pk},
        ),
        "discard_url": reverse(
            "machine_setup:plan_redesign_discard",
            kwargs={"project_id": project_id, "revision_id": revision.pk},
        ),
    }


def attachment_payload(document: SetupDocument) -> SetupAttachmentPayload:
    """Needs only the ``id``, ``file_name``, ``size_bytes`` and ``char_count``
    columns. The page builds the download link from ``document_url_template``."""
    return {
        "id": document.pk,
        "file_name": document.file_name[:255],
        "size_bytes": document.size_bytes,
        "char_count": document.char_count,
    }


def run_proposal_payload(proposal: SetupRunProposal) -> RunProposalPayload:
    """Never touches ``case_ids`` (deferred on display loads)."""
    runs: list[ProposedRunPayload] = []
    for row in _dict_rows(proposal.runs)[:_PROPOSAL_ROWS_LIMIT]:
        runs.append(
            {
                "name": str(row.get("name", ""))[:RUN_NAME_MAX],
                "case_count": _int(row.get("case_count")),
                "sample_titles": [
                    str(title)[:_SAMPLE_TITLE_LIMIT]
                    for title in _list(row.get("sample_titles"))[:SAMPLE_TITLES]
                ],
                "name_exists": bool(row.get("name_exists")),
            }
        )
    created: list[CreatedRunPayload] = []
    for row in _dict_rows(proposal.created_runs)[:_PROPOSAL_ROWS_LIMIT]:
        run_id = _int(row.get("run_id"))
        created.append(
            {
                "name": str(row.get("name", ""))[:RUN_NAME_MAX],
                "case_count": _int(row.get("case_count")),
                "url": reverse(
                    "projects:test_run_detail",
                    kwargs={"project_id": proposal.project_id, "test_run_id": run_id},
                ),
            }
        )
    return {
        "id": proposal.pk,
        "status": proposal.status,
        "status_label": proposal.get_status_display(),
        "note": proposal.note[:PROPOSAL_NOTE_MAX],
        "runs": runs,
        "created_runs": created,
        "skipped": [
            str(name)[:RUN_NAME_MAX]
            for name in _list(proposal.skipped)[:_PROPOSAL_ROWS_LIMIT]
        ],
    }


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _dict_rows(value: object) -> list[dict[str, object]]:
    return [row for row in _list(value) if isinstance(row, dict)]


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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
    # The recommended ``HelpContinueMode`` value and its label; "" when none.
    continue_mode: str
    continue_mode_label: str


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
    # The fault's label; empty when the tester asked for help.
    fault_label: str
    # A ``projects.models.HelpContinueMode`` value: how Continue goes on.
    continue_mode: str


def help_thread_payload(
    *,
    conversation_id: int,
    request_open: bool,
    resolution_label: str,
    proposal_action: str,
    proposal_action_label: str,
    proposal_note: str,
    draft: PlanDraftView | None,
    fault_label: str,
    continue_mode: str,
    proposal_continue_mode: str = "",
    proposal_continue_mode_label: str = "",
) -> HelpThreadPayload:
    return {
        "conversation_id": conversation_id,
        "request_open": request_open,
        "resolution_label": resolution_label,
        "fault_label": fault_label,
        "continue_mode": continue_mode,
        "agent_proposal": (
            {
                "action": proposal_action,
                "action_label": proposal_action_label,
                "note": proposal_note[:_HELP_NOTE_LIMIT],
                "continue_mode": proposal_continue_mode,
                "continue_mode_label": proposal_continue_mode_label,
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
