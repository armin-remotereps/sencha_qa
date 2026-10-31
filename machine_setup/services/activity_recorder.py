"""Write and broadcast the chat rows of one tool loop (main agent or sub-task).

The main agent's loop and every sub-task loop of a turn each get their own
recorder; a sub-task's recorder stamps its rows with the delegation row as
``parent``.
"""

from __future__ import annotations

import base64
import logging
import time

from django.core.files.base import ContentFile
from django.db.models import Case, F, Prefetch, Value, When
from django.utils import timezone

from agents.types import (
    ChatMessage,
    ToolCall,
    ToolContext,
    ToolDispatcher,
    ToolResult,
)
from machine_setup.events import broadcast_message, broadcast_screenshot
from machine_setup.message_text import message_text
from machine_setup.models import (
    SetupMessage,
    SetupMessageKind,
    SetupRunProposal,
    SetupScreenshot,
    SetupSubtaskResult,
    SetupToolStatus,
    SetupTurn,
    SetupTurnStatus,
)
from machine_setup.payloads import (
    CARD_DEFER,
    CARD_SELECT_RELATED,
    message_payload,
    screenshot_payload,
)
from machine_setup.subtask_types import RUN_SETUP_TASK_TOOL

logger = logging.getLogger(__name__)

TOOL_PREVIEW_LIMIT = 4000
# Text of a tool row closed because its turn ended (stopped, failed, worker lost).
TOOL_INTERRUPTED_TEXT = "The turn ended before this tool finished."


class ActivityRecorder:
    """Rows of one loop: assistant text, tool calls and their screenshots."""

    def __init__(
        self,
        *,
        project_id: int,
        conversation_id: int,
        turn_id: int,
        parent: SetupMessage | None,
        record_final_reply: bool,
    ) -> None:
        self.project_id = project_id
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.parent = parent
        # False for a sub-task: its final report lands on the delegation row.
        self.record_final_reply = record_final_reply
        # This loop's tool rows by call id, with the screenshots attached so far.
        self.tool_rows: dict[str, SetupMessage] = {}
        self.row_screenshots: dict[int, list[SetupScreenshot]] = {}
        # Raw results by call id: the loop hands the hook only the (possibly
        # capped) model-facing text, without the error flag.
        self.results: dict[str, ToolResult] = {}
        self.current_call_id: str | None = None
        # Calls of the last assistant message still waiting for their result.
        self.pending_call_ids: set[str] = set()

    @property
    def pending_calls(self) -> bool:
        return bool(self.pending_call_ids)

    def wrap(self, dispatch: ToolDispatcher) -> ToolDispatcher:
        def _dispatch(tool_call: ToolCall, context: ToolContext) -> ToolResult:
            self.current_call_id = tool_call.tool_call_id
            try:
                result = dispatch(tool_call, context)
            finally:
                self.current_call_id = None
            self.results[tool_call.tool_call_id] = result
            return result

        return _dispatch

    def current_row(self) -> SetupMessage | None:
        if self.current_call_id is None:
            return None
        return self.tool_rows.get(self.current_call_id)

    def on_message(self, message: ChatMessage) -> None:
        if message.role == "assistant":
            self.pending_call_ids = {
                call.tool_call_id for call in message.tool_calls or ()
            }
            self._record_assistant(message)
        elif message.role == "tool" and message.tool_call_id is not None:
            self.pending_call_ids.discard(message.tool_call_id)
            self._record_tool_result(message.tool_call_id, message)

    def _record_assistant(self, message: ChatMessage) -> None:
        calls = message.tool_calls or ()
        text = message_text(message)
        if text and (calls or self.record_final_reply):
            self.create_row(kind=SetupMessageKind.ASSISTANT, text=text)
        if not calls:
            return
        rows = SetupMessage.objects.bulk_create(
            [
                SetupMessage(
                    conversation_id=self.conversation_id,
                    turn_id=self.turn_id,
                    parent=self.parent,
                    kind=SetupMessageKind.TOOL,
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    tool_arguments=call.arguments,
                    tool_status=SetupToolStatus.RUNNING,
                )
                for call in calls
            ]
        )
        for row in rows:
            self.tool_rows[row.tool_call_id] = row
            self.row_screenshots[row.pk] = []
            self.broadcast_row(row)

    def _record_tool_result(self, call_id: str, message: ChatMessage) -> None:
        row = self.tool_rows.get(call_id)
        result = self.results.pop(call_id, None)
        if row is None:
            return
        row.tool_status = (
            SetupToolStatus.FAILED
            if result is not None and result.is_error
            else SetupToolStatus.SUCCEEDED
        )
        text = result.content if result is not None else message_text(message)
        # A delegation row shows the sub-task report, already capped by
        # subtask_report.REPORT_MAX_CHARS.
        row.text = text if row.tool_name == RUN_SETUP_TASK_TOOL else _preview(text)
        row.save(update_fields=["tool_status", "text", "updated_at"])
        self.broadcast_row(row)

    def on_screenshot(self, base64_data: str, tool_name: str) -> None:
        row = self.current_row()
        if row is None:
            logger.debug("Setup turn %s: screenshot outside a tool call", self.turn_id)
            return
        # A controller call that returns after the turn was closed (force
        # stop, worker_lost) adds nothing to the closed turn.
        if not SetupTurn.objects.filter(
            pk=self.turn_id, status=SetupTurnStatus.RUNNING
        ).exists():
            return
        timestamp_ms = int(time.time() * 1000)
        screenshot = SetupScreenshot.objects.create(
            message=row,
            image=ContentFile(
                base64.b64decode(base64_data), name=f"{timestamp_ms}_{tool_name}.png"
            ),
            tool_name=tool_name,
        )
        self.row_screenshots.setdefault(row.pk, []).append(screenshot)
        broadcast_screenshot(self.project_id, row.pk, screenshot_payload(screenshot))

    def create_row(
        self,
        *,
        kind: SetupMessageKind,
        text: str,
        link_url: str = "",
        run_proposal_id: int | None = None,
        result_change_id: int | None = None,
        plan_revision_id: int | None = None,
    ) -> SetupMessage:
        """``run_proposal_id`` makes the row a run proposal card;
        ``result_change_id`` / ``plan_revision_id`` a result-chat card."""
        row = SetupMessage.objects.create(
            conversation_id=self.conversation_id,
            turn_id=self.turn_id,
            parent=self.parent,
            kind=kind,
            text=text,
            link_url=link_url,
            run_proposal_id=run_proposal_id,
            result_change_id=result_change_id,
            plan_revision_id=plan_revision_id,
        )
        if run_proposal_id is not None:
            # One query for the card; case_ids can be large and is not shown.
            row.run_proposal = (
                SetupRunProposal.objects.defer("case_ids")
                .filter(pk=run_proposal_id)
                .first()
            )
        if result_change_id is not None or plan_revision_id is not None:
            # One query for the card with its case (log deferred).
            loaded = (
                SetupMessage.objects.select_related(*CARD_SELECT_RELATED)
                .defer(*CARD_DEFER)
                .filter(pk=row.pk)
                .first()
            )
            if loaded is not None:
                row.result_change = loaded.result_change
                row.plan_revision = loaded.plan_revision
        self.broadcast_row(row)
        return row

    def broadcast_row(self, row: SetupMessage) -> None:
        broadcast_message(
            self.project_id,
            message_payload(
                row,
                screenshots=self.row_screenshots.get(row.pk, []),
                project_id=self.project_id,
            ),
        )


def fail_running_rows(*, turn_id: int, text: str) -> list[SetupMessage]:
    """Fail the turn's running tool rows, nested ones included.

    A running ``run_setup_task`` row without a result becomes ``interrupted``.
    Returns the changed rows (screenshots prefetched) for the caller to
    broadcast.
    """
    running = SetupMessage.objects.filter(
        turn_id=turn_id, tool_status=SetupToolStatus.RUNNING
    )
    ids = list(running.values_list("pk", flat=True))
    if not ids:
        return []
    # Rechecked in the UPDATE: a worker judged lost may still finish a row.
    SetupMessage.objects.filter(pk__in=ids, tool_status=SetupToolStatus.RUNNING).update(
        tool_status=SetupToolStatus.FAILED,
        text=text,
        subtask_result=Case(
            When(
                tool_name=RUN_SETUP_TASK_TOOL,
                subtask_result="",
                then=Value(SetupSubtaskResult.INTERRUPTED),
            ),
            default=F("subtask_result"),
        ),
        updated_at=timezone.now(),
    )
    # Tool rows have no author, so no author join.
    return list(
        SetupMessage.objects.filter(pk__in=ids).prefetch_related(
            Prefetch("screenshots", queryset=SetupScreenshot.objects.order_by("id"))
        )
    )


def _preview(text: str) -> str:
    if len(text) > TOOL_PREVIEW_LIMIT:
        return text[:TOOL_PREVIEW_LIMIT] + "\n…[truncated]"
    return text
