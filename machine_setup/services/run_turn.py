"""Run one machine setup turn in the worker: replay the chat, loop, record.

The LLM transcript (replayed exactly, reasoning included) and the display rows
(what the page shows) are written side by side from the tool loop's
``on_transcript`` hook, and every row is broadcast as it changes.
"""

from __future__ import annotations

import base64
import logging
import time

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from agents.services.agent_loop import run_tool_loop
from agents.services.llm_config import build_llm_config
from agents.transcript import (
    close_dangling_tool_calls,
    transcript_from_json,
    transcript_to_json,
)
from agents.types import (
    AgentConfig,
    BrowserSelection,
    ChatMessage,
    TextContent,
    ToolCall,
    ToolContext,
    ToolDispatcher,
    ToolResult,
)
from machine_setup.events import (
    broadcast_message,
    broadcast_screenshot,
    broadcast_turn,
)
from machine_setup.models import (
    SetupConversation,
    SetupMessage,
    SetupMessageKind,
    SetupScreenshot,
    SetupToolStatus,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.payloads import message_payload, screenshot_payload, turn_payload
from machine_setup.services.setup_prompt import build_setup_system_prompt
from machine_setup.services.setup_tools import (
    build_setup_dispatcher,
    setup_tool_definitions,
)
from machine_setup.turn_outcome import (
    TurnOutcome,
    classify_turn_end,
    stopped_outcome,
    transcript_invalid_outcome,
)
from projects.controller_outage_clock import reset_outage_clock
from projects.services import build_agent_project_context

logger = logging.getLogger(__name__)

TOOL_PREVIEW_LIMIT = 4000
_PREVIOUS_TURN_STOPPED_NOTE = "Not run: the previous turn was stopped."
_TURN_STOPPED_NOTE = "Not run: the turn was stopped."
_TOOL_INTERRUPTED_TEXT = "The turn ended before this tool finished."


def run_setup_turn(turn_id: int) -> None:
    """Claim a queued turn and run it to an end state.

    Returns at once when the turn is no longer queued (a redelivered task, or
    a turn Stop or Reset already closed).
    """
    reset_outage_clock()
    claimed = SetupTurn.objects.filter(
        pk=turn_id, status=SetupTurnStatus.QUEUED
    ).update(status=SetupTurnStatus.RUNNING, started_at=timezone.now())
    if not claimed:
        logger.info("Setup turn %s is not queued; nothing to run", turn_id)
        return
    turn = SetupTurn.objects.select_related(
        "conversation", "project", "requested_by"
    ).get(pk=turn_id)
    logger.info(
        "Setup turn %s started (project %s, conversation %s)",
        turn.pk,
        turn.project_id,
        turn.conversation_id,
    )
    _TurnRunner(turn).run()


class _TurnRunner:
    """State of one running turn, shared by the tool loop's callbacks."""

    def __init__(self, turn: SetupTurn) -> None:
        self.turn = turn
        self.project_id = turn.project_id
        self.conversation_id = turn.conversation_id
        self.iterations = 0
        # Transcript without the system prompt; None until it was loaded.
        self.transcript: tuple[ChatMessage, ...] | None = None
        # This turn's tool rows by call id, with the screenshots attached so far.
        self.tool_rows: dict[str, SetupMessage] = {}
        self.row_screenshots: dict[int, list[SetupScreenshot]] = {}
        # Raw results by call id: the loop hands the hook only the (possibly
        # summarized) model-facing text, without the error flag.
        self.results: dict[str, ToolResult] = {}
        self.current_call_id: str | None = None
        # Calls of the last assistant message still waiting for their result.
        self.pending_call_ids: set[str] = set()

    def run(self) -> None:
        outcome: TurnOutcome | None = None
        try:
            outcome = self._execute()
        except Exception as exc:
            outcome = classify_turn_end(
                exception=exc, conversation_archived=self._conversation_archived()
            )
            _log_turn_exception(self.turn.pk, exc, outcome)
        finally:
            if outcome is None:
                outcome = classify_turn_end(exception=RuntimeError("turn aborted"))
            self._finish(outcome)

    def _execute(self) -> TurnOutcome:
        turn = self.turn
        broadcast_turn(self.project_id, turn_payload(turn))
        if turn.stop_requested or turn.conversation.archived_at is not None:
            return stopped_outcome(
                conversation_archived=turn.conversation.archived_at is not None
            )
        history = transcript_from_json(turn.conversation.llm_transcript)
        if history is None:
            return transcript_invalid_outcome()
        user_text = (
            SetupMessage.objects.filter(turn_id=turn.pk, kind=SetupMessageKind.USER)
            .values_list("text", flat=True)
            .first()
        )
        if user_text is None:
            raise LookupError(f"Setup turn {turn.pk} has no user message")

        history = close_dangling_tool_calls(history, _PREVIOUS_TURN_STOPPED_NOTE) + (
            ChatMessage(role="user", content=user_text),
        )
        # Persisted up front so the message stays in the history even when the
        # turn fails before the model answers.
        self._persist_transcript(history)

        project = turn.project
        system_prompt = build_setup_system_prompt(
            system_info=project.agent_system_info or None,
            project_prompt=build_agent_project_context(project),
            max_iterations=settings.MACHINE_SETUP_MAX_ITERATIONS,
        )
        llm_config = build_llm_config()
        config = AgentConfig(
            llm=llm_config,
            vision_llm=llm_config,
            max_iterations=settings.MACHINE_SETUP_MAX_ITERATIONS,
            timeout_seconds=settings.MACHINE_SETUP_TURN_TIMEOUT_SECONDS,
            on_screenshot=self._on_screenshot,
            cancellation_check=self._should_stop,
        )
        context = ToolContext(
            project_id=self.project_id,
            browser_selection=BrowserSelection(),
            summarizer_config=build_llm_config(),
            vision_config=config.vision_llm,
            on_screenshot=self._on_screenshot,
            on_log=_log_tool_debug,
            cancellation_check=self._should_stop,
        )
        dispatch = build_setup_dispatcher(
            project_id=self.project_id,
            author=turn.requested_by,
            on_context_updated=self._on_context_updated,
        )
        result = run_tool_loop(
            [ChatMessage(role="system", content=system_prompt), *history],
            context,
            config=config,
            tool_definitions=setup_tool_definitions(),
            dispatch=self._recording(dispatch),
            on_transcript=self._on_transcript,
        )
        self.iterations = result.iterations
        return classify_turn_end(
            stop_reason=result.stop_reason, iterations=result.iterations
        )

    # Tool loop callbacks

    def _recording(self, dispatch: ToolDispatcher) -> ToolDispatcher:
        def _dispatch(tool_call: ToolCall, context: ToolContext) -> ToolResult:
            self.current_call_id = tool_call.tool_call_id
            try:
                result = dispatch(tool_call, context)
            finally:
                self.current_call_id = None
            self.results[tool_call.tool_call_id] = result
            return result

        return _dispatch

    def _on_transcript(
        self, message: ChatMessage, transcript: tuple[ChatMessage, ...]
    ) -> None:
        self.transcript = tuple(m for m in transcript if m.role != "system")
        if message.role == "assistant":
            self.pending_call_ids = {
                call.tool_call_id for call in message.tool_calls or ()
            }
            self._record_assistant(message)
        elif message.role == "tool" and message.tool_call_id is not None:
            self.pending_call_ids.discard(message.tool_call_id)
            self._record_tool_result(message.tool_call_id, message)
        # Two writes per round: the assistant message (so a killed worker
        # leaves calls the next turn's close_dangling_tool_calls can answer,
        # matching the rows the user saw) and the round's last result.
        # Rewriting after every tool result would be quadratic in bytes.
        if message.role == "assistant" or not self.pending_call_ids:
            self._persist_transcript(self.transcript)

    def _record_assistant(self, message: ChatMessage) -> None:
        self.iterations += 1
        SetupTurn.objects.filter(pk=self.turn.pk).update(iterations=self.iterations)
        self.turn.iterations = self.iterations
        broadcast_turn(self.project_id, turn_payload(self.turn))

        text = _message_text(message)
        if text:
            self._create_row(kind=SetupMessageKind.ASSISTANT, text=text)
        calls = message.tool_calls or ()
        if not calls:
            return
        rows = SetupMessage.objects.bulk_create(
            [
                SetupMessage(
                    conversation_id=self.conversation_id,
                    turn_id=self.turn.pk,
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
            self._broadcast_row(row)

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
        row.text = _preview(
            result.content if result is not None else _message_text(message)
        )
        row.save(update_fields=["tool_status", "text", "updated_at"])
        self._broadcast_row(row)

    def _on_screenshot(self, base64_data: str, tool_name: str) -> None:
        row = (
            self.tool_rows.get(self.current_call_id)
            if self.current_call_id is not None
            else None
        )
        if row is None:
            logger.debug("Setup turn %s: screenshot outside a tool call", self.turn.pk)
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

    def _on_context_updated(self, revision: int, change_summary: str) -> None:
        logger.info(
            "Setup turn %s wrote application context revision %s",
            self.turn.pk,
            revision,
        )
        self._create_row(
            kind=SetupMessageKind.NOTICE,
            text=f"Application context updated: {change_summary}",
            link_url=reverse(
                "projects:application_context",
                kwargs={"project_id": self.project_id},
            ),
        )

    def _should_stop(self) -> bool:
        return SetupTurn.objects.filter(
            Q(stop_requested=True) | Q(conversation__archived_at__isnull=False),
            pk=self.turn.pk,
        ).exists()

    # Ending

    def _finish(self, outcome: TurnOutcome) -> None:
        """Bring the turn to its end state; each step is best effort on its own."""
        try:
            self._close_transcript()
        except Exception:
            logger.exception(
                "Setup turn %s: closing the transcript failed", self.turn.pk
            )
        try:
            self._fail_running_tool_rows()
        except Exception:
            logger.exception("Setup turn %s: closing tool rows failed", self.turn.pk)
        if outcome.notice:
            try:
                self._create_row(kind=SetupMessageKind.NOTICE, text=outcome.notice)
            except Exception:
                logger.exception(
                    "Setup turn %s: writing the notice failed", self.turn.pk
                )

        finished_at = timezone.now()
        # Conditional: a stale turn may already have been closed as worker_lost.
        SetupTurn.objects.filter(
            pk=self.turn.pk, status=SetupTurnStatus.RUNNING
        ).update(
            status=outcome.status,
            ended_reason=outcome.ended_reason,
            error_message=outcome.error_message,
            iterations=self.iterations,
            finished_at=finished_at,
        )
        self.turn.status = outcome.status
        self.turn.ended_reason = outcome.ended_reason
        self.turn.error_message = outcome.error_message
        self.turn.iterations = self.iterations
        self.turn.finished_at = finished_at
        logger.info(
            "Setup turn %s ended: %s/%s after %s steps",
            self.turn.pk,
            outcome.status,
            outcome.ended_reason,
            self.iterations,
        )
        try:
            broadcast_turn(self.project_id, turn_payload(self.turn))
        except Exception:
            logger.exception("Setup turn %s: final broadcast failed", self.turn.pk)

    def _close_transcript(self) -> None:
        if self.transcript is None:
            return
        closed = close_dangling_tool_calls(self.transcript, _TURN_STOPPED_NOTE)
        if len(closed) != len(self.transcript):
            self.transcript = closed
            self._persist_transcript(closed)

    def _fail_running_tool_rows(self) -> None:
        running = [
            row
            for row in self.tool_rows.values()
            if row.tool_status == SetupToolStatus.RUNNING
        ]
        if not running:
            return
        SetupMessage.objects.filter(pk__in=[row.pk for row in running]).update(
            tool_status=SetupToolStatus.FAILED,
            text=_TOOL_INTERRUPTED_TEXT,
            updated_at=timezone.now(),
        )
        for row in running:
            row.tool_status = SetupToolStatus.FAILED
            row.text = _TOOL_INTERRUPTED_TEXT
            self._broadcast_row(row)

    # Persistence helpers

    def _persist_transcript(self, transcript: tuple[ChatMessage, ...]) -> None:
        SetupConversation.objects.filter(pk=self.conversation_id).update(
            llm_transcript=transcript_to_json(transcript), updated_at=timezone.now()
        )

    def _create_row(
        self, *, kind: SetupMessageKind, text: str, link_url: str = ""
    ) -> SetupMessage:
        row = SetupMessage.objects.create(
            conversation_id=self.conversation_id,
            turn_id=self.turn.pk,
            kind=kind,
            text=text,
            link_url=link_url,
        )
        self._broadcast_row(row)
        return row

    def _broadcast_row(self, row: SetupMessage) -> None:
        broadcast_message(
            self.project_id,
            message_payload(row, screenshots=self.row_screenshots.get(row.pk, [])),
        )

    def _conversation_archived(self) -> bool:
        return SetupConversation.objects.filter(
            pk=self.conversation_id, archived_at__isnull=False
        ).exists()


def _message_text(message: ChatMessage) -> str:
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    return "\n".join(
        part.text for part in content if isinstance(part, TextContent)
    ).strip()


def _preview(text: str) -> str:
    if len(text) > TOOL_PREVIEW_LIMIT:
        return text[:TOOL_PREVIEW_LIMIT] + "\n…[truncated]"
    return text


def _log_tool_debug(message: str) -> None:
    # Tool output can hold secrets the user typed; never above DEBUG.
    logger.debug(message)


def _log_turn_exception(turn_id: int, exc: Exception, outcome: TurnOutcome) -> None:
    if outcome.ended_reason == SetupTurnEndReason.INTERNAL_ERROR:
        logger.exception("Setup turn %s failed", turn_id)
    else:
        logger.warning(
            "Setup turn %s ended (%s) by %s: %s",
            turn_id,
            outcome.ended_reason,
            type(exc).__name__,
            exc,
        )
