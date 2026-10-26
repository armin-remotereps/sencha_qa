from __future__ import annotations

import logging
from dataclasses import replace

from django.conf import settings

from agents.services.llm_client import send_chat_completion
from agents.services.llm_config import (
    build_llm_config,
)
from agents.services.prompt_parts import (
    build_agent_persona,
    build_environment_context,
    build_project_context,
    build_qa_rules,
    build_tool_guidelines,
    build_tool_taxonomy,
)
from agents.services.tool_registry import dispatch_tool_call, get_all_tool_definitions
from agents.types import (
    AgentCancelledError,
    AgentConfig,
    AgentResult,
    AgentStopReason,
    BrowserSelection,
    CancellationCheck,
    ChatMessage,
    LogCallback,
    ToolContext,
    ToolDefinition,
    ToolDispatcher,
    TranscriptCallback,
)
from projects.controller_outage_clock import agent_monotonic

logger = logging.getLogger(__name__)

# The [Tool Result] debug line reaches the case log and its WebSocket; the
# model gets the full (capped) result.
_TOOL_RESULT_LOG_PREVIEW_CHARS = 2000


def _check_cancellation(cancellation_check: CancellationCheck | None) -> None:
    if cancellation_check is not None and cancellation_check():
        raise AgentCancelledError("Agent cancelled by external request")


def _debug_log(message: str, on_log: LogCallback | None) -> None:
    logger.debug(message)
    if on_log is not None:
        on_log(message)


def build_system_prompt(
    task_description: str,
    *,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
) -> str:
    sections = [
        _build_role_description(system_info=system_info),
        build_tool_guidelines(),
    ]
    if project_prompt:
        sections.append(build_project_context(project_prompt))
    sections.append(_build_task_section(task_description))
    return "\n\n".join(sections)


def _build_role_description(
    *,
    system_info: dict[str, object] | None = None,
) -> str:
    return "\n\n".join(
        [
            build_agent_persona(system_info=system_info),
            build_qa_rules(),
            build_tool_taxonomy(),
            build_environment_context(system_info=system_info),
        ]
    )


def _build_task_section(task_description: str) -> str:
    return (
        "IMPORTANT:\n"
        "- For web tasks, use browser_navigate to open URLs — no need to launch a browser manually\n"
        "- Use browser_click/browser_type/browser_hover/browser_take_screenshot for anything "
        "inside a web page — never desktop click/take_screenshot for web page content\n"
        "- Use execute_command for shell operations\n"
        "- Use browser_take_screenshot (web pages) or take_screenshot (desktop) with specific "
        "questions to observe the environment\n"
        "- Use click/hover with descriptive element names for native desktop GUI interactions\n"
        "- Always check tool output and use screenshots to verify results\n"
        "- If a tool fails, try to diagnose and fix the issue\n\n"
        f"YOUR TASK:\n{task_description}"
    )


def build_agent_config() -> AgentConfig:
    llm_config = build_llm_config()
    return AgentConfig(
        llm=llm_config,
        vision_llm=llm_config,
        max_iterations=settings.AGENT_MAX_ITERATIONS,
        timeout_seconds=settings.AGENT_TIMEOUT_SECONDS,
    )


def run_agent(
    task_description: str,
    project_id: int,
    *,
    config: AgentConfig | None = None,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
) -> AgentResult:
    if config is None:
        config = build_agent_config()

    summarizer_config = build_llm_config()

    context = ToolContext(
        project_id=project_id,
        # Outside a test run there is no case to share a selection with.
        browser_selection=BrowserSelection(),
        summarizer_config=summarizer_config,
        vision_config=config.vision_llm,
        on_screenshot=config.on_screenshot,
        on_log=config.on_log,
        cancellation_check=config.cancellation_check,
    )
    return _run_agent_loop(
        task_description,
        context,
        config=config,
        system_info=system_info,
        project_prompt=project_prompt,
    )


def _run_agent_loop(
    task_description: str,
    context: ToolContext,
    *,
    config: AgentConfig,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
    system_prompt: str | None = None,
    tool_definitions: tuple[ToolDefinition, ...] | None = None,
    dispatch: ToolDispatcher | None = None,
) -> AgentResult:
    if system_prompt is None:
        system_prompt = build_system_prompt(
            task_description,
            system_info=system_info,
            project_prompt=project_prompt,
        )
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=task_description),
    ]
    return run_tool_loop(
        messages,
        context,
        config=config,
        tool_definitions=(
            get_all_tool_definitions() if tool_definitions is None else tool_definitions
        ),
        dispatch=dispatch_tool_call if dispatch is None else dispatch,
    )


def run_tool_loop(
    messages: list[ChatMessage],
    context: ToolContext,
    *,
    config: AgentConfig,
    tool_definitions: tuple[ToolDefinition, ...],
    dispatch: ToolDispatcher,
    on_transcript: TranscriptCallback | None = None,
) -> AgentResult:
    """Run the LLM/tool loop, continuing from ``messages`` (system prompt first).

    Shared by the tester's sub-agents and the machine setup chat, so the
    deadline, cancellation and tool-result cap rules exist once.
    """
    _debug_log(
        f"[Agent] Loaded {len(tool_definitions)} tool definitions", config.on_log
    )

    messages = list(messages)
    start_time = agent_monotonic()
    context = replace(context, deadline=start_time + config.timeout_seconds)
    iterations = 0
    tool_calls_made = 0
    on_log = config.on_log

    def _append(message: ChatMessage) -> None:
        messages.append(message)
        if on_transcript is not None:
            on_transcript(message, tuple(messages))

    while iterations < config.max_iterations:
        elapsed = agent_monotonic() - start_time
        if elapsed > config.timeout_seconds:
            logger.warning("Agent timed out after %.1f seconds", elapsed)
            return AgentResult(
                stop_reason=AgentStopReason.TIMEOUT,
                iterations=iterations,
                messages=tuple(messages),
                error=f"Timed out after {elapsed:.1f} seconds",
            )

        iterations += 1
        _check_cancellation(config.cancellation_check)
        _debug_log(f"Agent iteration {iterations}/{config.max_iterations}", on_log)

        # AIServiceError propagates: a provider fault is not a verdict on the
        # application, so the caller pauses the case for help.
        response = send_chat_completion(
            config.llm,
            tuple(messages),
            tool_definitions,
        )

        if response.reasoning_content:
            _debug_log(f"[Thinking] {response.reasoning_content}", on_log)
        if isinstance(response.message.content, str) and response.message.content:
            _debug_log(f"[Agent] {response.message.content}", on_log)

        _append(response.message)
        _check_cancellation(config.cancellation_check)

        if response.message.tool_calls is None:
            _debug_log(
                f"[Agent] No tool calls in response — completing "
                f"(total tool calls made: {tool_calls_made})",
                on_log,
            )
            return AgentResult(
                stop_reason=AgentStopReason.TASK_COMPLETE,
                iterations=iterations,
                messages=tuple(messages),
            )

        for tool_call in response.message.tool_calls:
            _check_cancellation(config.cancellation_check)
            tool_calls_made += 1
            _debug_log(
                f"[Tool Call] {tool_call.tool_name}({tool_call.arguments})", on_log
            )
            tool_result = dispatch(tool_call, context)

            content = _cap_tool_result(tool_result.content)
            _debug_log(f"[Tool Result] {_log_preview(content)}", on_log)
            _append(
                ChatMessage(
                    role="tool", content=content, tool_call_id=tool_result.tool_call_id
                )
            )

    logger.warning("Agent hit max iterations: %d", config.max_iterations)
    return AgentResult(
        stop_reason=AgentStopReason.MAX_ITERATIONS,
        iterations=iterations,
        messages=tuple(messages),
    )


def _cap_tool_result(content: str) -> str:
    """Keep a too-long result's head and tail, half the budget each.

    Even halves: a failing command's error and exit code are at the end, a
    page's main content at the start.
    """
    # At least one character, so a misconfigured setting cannot garble slices.
    limit = max(1, settings.AGENT_TOOL_RESULT_MAX_CHARS)
    total = len(content)
    if total <= limit:
        return content
    head = limit // 2
    tail = limit - head
    omitted = total - limit
    logger.info("[Agent] Tool result capped: %d of %d chars kept", limit, total)
    return (
        content[:head]
        + f"\n\n[... {omitted:,} of {total:,} characters omitted from the middle "
        "of this tool result ...]\n\n" + content[total - tail :]
    )


def _log_preview(content: str) -> str:
    if len(content) <= _TOOL_RESULT_LOG_PREVIEW_CHARS:
        return content
    return f"{content[:_TOOL_RESULT_LOG_PREVIEW_CHARS]}… ({len(content):,} chars total)"
