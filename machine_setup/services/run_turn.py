"""Run one machine setup turn in the worker: replay the chat, loop, record.

The LLM transcript (replayed exactly, reasoning included) and the display rows
(what the page shows) are written side by side from the tool loop's
``on_transcript`` hook, and every row is broadcast as it changes.
"""

from __future__ import annotations

import logging

from django.conf import settings
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
    ToolCall,
    ToolContext,
    ToolResult,
)
from machine_setup.document_text import AttachedDocumentNote, user_turn_content
from machine_setup.events import broadcast_message, broadcast_turn
from machine_setup.models import (
    SetupConversation,
    SetupDocument,
    SetupMessage,
    SetupMessageKind,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.payloads import message_payload, turn_payload
from machine_setup.services.activity_recorder import (
    TOOL_INTERRUPTED_TEXT,
    ActivityRecorder,
    fail_running_rows,
)
from machine_setup.services.auto_fix_settlement import settle_case_auto_fix
from machine_setup.services.case_help_context import describe_help_status
from machine_setup.services.run_setup_task import SetupTaskRuntime, run_setup_task
from machine_setup.services.setup_prompt import build_setup_system_prompt
from machine_setup.services.setup_tools import (
    build_setup_dispatcher,
    main_tool_definitions,
)
from machine_setup.services.turn_reply_notification import notify_turn_reply
from machine_setup.subtask_types import SetupTaskRequest
from machine_setup.thread_role import ThreadRole, thread_role
from machine_setup.turn_outcome import (
    TurnOutcome,
    classify_turn_end,
    stopped_outcome,
    transcript_invalid_outcome,
)
from projects.controller_outage_clock import reset_outage_clock
from projects.services import build_agent_project_context

logger = logging.getLogger(__name__)

# Said for any open call, including a sub-task that may have changed the
# machine before Stop or a worker loss.
_INTERRUPTED_CALL_NOTE = (
    "Interrupted: the turn ended before this call returned. It may have partly "
    "run; check the machine before relying on it."
)
_ONE_SUBTASK_AT_A_TIME = (
    "Not run: delegate one sub-task at a time. Read the first report, then call "
    "again."
)


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
        self.recorder = ActivityRecorder(
            project_id=self.project_id,
            conversation_id=self.conversation_id,
            turn_id=turn.pk,
            parent=None,
            record_final_reply=True,
        )
        # Built in _execute; every sub-task of the turn shares it.
        self.subtask_runtime: SetupTaskRuntime | None = None
        # Whether the current assistant message already ran a sub-task.
        self.subtask_ran_this_round = False

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
        user_row = (
            SetupMessage.objects.filter(turn_id=turn.pk, kind=SetupMessageKind.USER)
            .values("id", "text")
            .first()
        )
        if user_row is None:
            raise LookupError(f"Setup turn {turn.pk} has no user message")

        # The note names each document's id, so later turns can still read it.
        history = close_dangling_tool_calls(history, _INTERRUPTED_CALL_NOTE) + (
            ChatMessage(
                role="user",
                content=user_turn_content(
                    user_row["text"], _attached_documents(user_row["id"])
                ),
            ),
        )
        # Persisted up front so the message stays in the history even when the
        # turn fails before the model answers.
        self._persist_transcript(history)

        project = turn.project
        system_info = project.agent_system_info or None
        project_prompt = build_agent_project_context(project)
        help_request_id = turn.conversation.help_request_id
        role = thread_role(auto_fix_turn=turn.auto_fix, help_request_id=help_request_id)
        system_prompt = build_setup_system_prompt(
            system_info=system_info,
            project_prompt=project_prompt,
            max_iterations=settings.MACHINE_SETUP_MAX_ITERATIONS,
            role=role,
            help_status=(
                describe_help_status(help_request_id)
                if role == ThreadRole.CASE_HELP and help_request_id is not None
                else ""
            ),
        )
        llm_config = build_llm_config()
        self.subtask_runtime = SetupTaskRuntime(
            project_id=self.project_id,
            conversation_id=self.conversation_id,
            turn_id=turn.pk,
            system_info=system_info,
            project_prompt=project_prompt,
            llm_config=llm_config,
            cancellation_check=self._should_stop,
        )
        config = AgentConfig(
            llm=llm_config,
            vision_llm=llm_config,
            max_iterations=settings.MACHINE_SETUP_MAX_ITERATIONS,
            timeout_seconds=settings.MACHINE_SETUP_TURN_TIMEOUT_SECONDS,
            on_screenshot=self.recorder.on_screenshot,
            cancellation_check=self._should_stop,
        )
        context = ToolContext(
            project_id=self.project_id,
            browser_selection=BrowserSelection(),
            summarizer_config=build_llm_config(),
            vision_config=config.vision_llm,
            on_screenshot=self.recorder.on_screenshot,
            on_log=_log_tool_debug,
            cancellation_check=self._should_stop,
        )
        dispatch = build_setup_dispatcher(
            project=project,
            author=turn.requested_by,
            on_context_updated=self._on_context_updated,
            on_runs_proposed=self._on_runs_proposed,
            run_subtask=self._run_subtask,
            role=role,
            conversation_id=self.conversation_id,
            turn_id=turn.pk,
            help_request_id=help_request_id,
        )
        result = run_tool_loop(
            [ChatMessage(role="system", content=system_prompt), *history],
            context,
            config=config,
            tool_definitions=main_tool_definitions(role),
            dispatch=self.recorder.wrap(dispatch),
            on_transcript=self._on_transcript,
        )
        self.iterations = result.iterations
        return classify_turn_end(
            stop_reason=result.stop_reason, iterations=result.iterations
        )

    # Tool loop callbacks

    def _on_transcript(
        self, message: ChatMessage, transcript: tuple[ChatMessage, ...]
    ) -> None:
        self.transcript = tuple(m for m in transcript if m.role != "system")
        if message.role == "assistant":
            self.subtask_ran_this_round = False
            self._count_iteration()
        self.recorder.on_message(message)
        # Two writes per round: the assistant message (so a killed worker
        # leaves calls the next turn's close_dangling_tool_calls can answer,
        # matching the rows the user saw) and the round's last result.
        # Rewriting after every tool result would be quadratic in bytes.
        if message.role == "assistant" or not self.recorder.pending_calls:
            self._persist_transcript(self.transcript)

    def _count_iteration(self) -> None:
        self.iterations += 1
        SetupTurn.objects.filter(pk=self.turn.pk).update(iterations=self.iterations)
        self.turn.iterations = self.iterations
        broadcast_turn(self.project_id, turn_payload(self.turn))

    def _run_subtask(
        self, tool_call: ToolCall, request: SetupTaskRequest, context: ToolContext
    ) -> ToolResult:
        # Parallel tool calls can hold two delegations; the second goal was
        # chosen without the first report.
        if self.subtask_ran_this_round:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                content=_ONE_SUBTASK_AT_A_TIME,
                is_error=True,
            )
        if self.subtask_runtime is None:
            raise RuntimeError("Sub-task runtime used before the turn started")
        self.subtask_ran_this_round = True
        return run_setup_task(
            tool_call,
            request,
            context,
            runtime=self.subtask_runtime,
            parent_row=self.recorder.current_row(),
        )

    def _on_context_updated(self, revision: int, change_summary: str) -> None:
        logger.info(
            "Setup turn %s wrote application context revision %s",
            self.turn.pk,
            revision,
        )
        self.recorder.create_row(
            kind=SetupMessageKind.NOTICE,
            text=f"Application context updated: {change_summary}",
            link_url=reverse(
                "projects:application_context",
                kwargs={"project_id": self.project_id},
            ),
        )

    def _on_runs_proposed(self, proposal_id: int, run_count: int) -> None:
        logger.info(
            "Setup turn %s proposed %s test run(s) (proposal %s)",
            self.turn.pk,
            run_count,
            proposal_id,
        )
        self.recorder.create_row(
            kind=SetupMessageKind.NOTICE,
            text=f"Proposed {run_count} test run{'s' if run_count != 1 else ''}",
            run_proposal_id=proposal_id,
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
                self.recorder.create_row(
                    kind=SetupMessageKind.NOTICE, text=outcome.notice
                )
            except Exception:
                logger.exception(
                    "Setup turn %s: writing the notice failed", self.turn.pk
                )

        finished_at = timezone.now()
        # Conditional: a stale turn may already have been closed as worker_lost
        # (and then settled there).
        ended = SetupTurn.objects.filter(
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
        if ended:
            # Last: the turn is no longer live, so the run's gates pass.
            # Never raises.
            settle_case_auto_fix(self.turn)
            # Never raises.
            notify_turn_reply(self.turn, notice=outcome.notice)

    def _close_transcript(self) -> None:
        if self.transcript is None:
            return
        closed = close_dangling_tool_calls(self.transcript, _INTERRUPTED_CALL_NOTE)
        if len(closed) != len(self.transcript):
            self.transcript = closed
            self._persist_transcript(closed)

    def _fail_running_tool_rows(self) -> None:
        # Nested rows belong to sub-task recorders: broadcast each row with
        # its prefetched screenshots, not the main recorder's.
        for row in fail_running_rows(turn_id=self.turn.pk, text=TOOL_INTERRUPTED_TEXT):
            broadcast_message(self.project_id, message_payload(row))

    # Persistence helpers

    def _persist_transcript(self, transcript: tuple[ChatMessage, ...]) -> None:
        SetupConversation.objects.filter(pk=self.conversation_id).update(
            llm_transcript=transcript_to_json(transcript), updated_at=timezone.now()
        )

    def _conversation_archived(self) -> bool:
        return SetupConversation.objects.filter(
            pk=self.conversation_id, archived_at__isnull=False
        ).exists()


def _attached_documents(message_id: int) -> list[AttachedDocumentNote]:
    """The documents of the turn's USER row, without their text; one query."""
    return [
        AttachedDocumentNote(
            document_id=row["id"],
            file_name=row["file_name"],
            char_count=row["char_count"],
        )
        for row in SetupDocument.objects.filter(message_id=message_id)
        .order_by("id")
        .values("id", "file_name", "char_count")
    ]


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
