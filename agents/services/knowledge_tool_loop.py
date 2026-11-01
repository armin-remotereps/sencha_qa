"""A bounded knowledge base look-up loop that ends with one tool-free answer.

Shared by the planner and the Human in the loop guide research: at most
``max_rounds`` LLM calls whose ``search_knowledge`` / ``read_knowledge`` calls
all run (one case-log line each), within ``timeout_seconds``; when the model
answers without tools that text is the answer, otherwise one more call with
``tool_choice="none"`` asks for it with ``final_prompt``.
"""

from __future__ import annotations

import json
import logging

from agents.case_log import log_line
from agents.services.agent_loop import run_tool_loop
from agents.services.llm_client import send_chat_completion
from agents.services.tool_definitions import get_knowledge_tool_definitions
from agents.services.tool_registry import allowlisted_dispatcher
from agents.types import (
    AgentConfig,
    AgentStopReason,
    BrowserSelection,
    CancellationCheck,
    ChatMessage,
    LLMConfig,
    LogCallback,
    ToolCall,
    ToolContext,
    ToolDispatcher,
    ToolResult,
)

logger = logging.getLogger(__name__)

_KB_LOG_QUERY_MAX_CHARS = 200


def run_knowledge_tool_loop(
    llm: LLMConfig,
    messages: tuple[ChatMessage, ...],
    *,
    project_id: int,
    max_rounds: int,
    timeout_seconds: int,
    final_prompt: str,
    ended_note: str,
    log_prefix: str,
    on_log: LogCallback | None,
    cancellation_check: CancellationCheck | None,
) -> str:
    """The model's answer after at most ``max_rounds`` look-up rounds.

    Each look-up is logged as ``<log_prefix> KB: …``; when the rounds or the
    time run out, ``<log_prefix> Knowledge base look-ups ended (<reason>);
    <ended_note>`` is logged before the final call. ``AIServiceError`` and
    ``AgentCancelledError`` propagate.
    """
    tools = get_knowledge_tool_definitions()
    names = frozenset(tool.name for tool in tools)
    config = AgentConfig(
        llm=llm,
        # One round is one call: the last round's results are answered by the
        # final call below, so at most ROUNDS + 1 calls in all.
        max_iterations=max_rounds,
        timeout_seconds=timeout_seconds,
        # The loop's own debug lines (thinking, full tool results) stay out of
        # the case log; the dispatcher logs one line per look-up instead.
        on_log=None,
        cancellation_check=cancellation_check,
    )
    result = run_tool_loop(
        list(messages),
        ToolContext(project_id=project_id, browser_selection=BrowserSelection()),
        config=config,
        tool_definitions=tools,
        dispatch=_logged_dispatcher(
            allowlisted_dispatcher(names),
            names=names,
            log_prefix=log_prefix,
            on_log=on_log,
        ),
    )
    if result.stop_reason == AgentStopReason.TASK_COMPLETE:
        return _text(result.messages[-1])

    log_line(
        on_log,
        f"{log_prefix} Knowledge base look-ups ended "
        f"({result.stop_reason.value}); {ended_note}",
        logger,
    )
    response = send_chat_completion(
        llm,
        (*result.messages, ChatMessage(role="user", content=final_prompt)),
        tools,
        tool_choice="none",
    )
    return _text(response.message)


def _logged_dispatcher(
    dispatch: ToolDispatcher,
    *,
    names: frozenset[str],
    log_prefix: str,
    on_log: LogCallback | None,
) -> ToolDispatcher:
    """``dispatch`` with one case-log line per knowledge base look-up."""

    def _dispatch(tool_call: ToolCall, context: ToolContext) -> ToolResult:
        if tool_call.tool_name in names:
            log_line(
                on_log,
                f"{log_prefix} KB: {_describe_knowledge_call(tool_call)}",
                logger,
            )
        return dispatch(tool_call, context)

    return _dispatch


def _describe_knowledge_call(tool_call: ToolCall) -> str:
    arguments = tool_call.arguments
    if "query" in arguments:
        query = str(arguments["query"])[:_KB_LOG_QUERY_MAX_CHARS]
        return f"{tool_call.tool_name} {json.dumps(query, ensure_ascii=False)}"
    page = arguments.get("page")
    page_part = "" if page is None else f" page {page}"
    return f"{tool_call.tool_name} id={arguments.get('item_id')}{page_part}"


def _text(message: ChatMessage) -> str:
    return message.content if isinstance(message.content, str) else ""
