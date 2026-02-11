from __future__ import annotations

import logging
import time

from django.conf import settings

from agents.services.agent_resource_manager import AgentResourceManager
from agents.services.dmr_client import (
    build_dmr_config,
    build_summarizer_config,
    build_vision_dmr_config,
    ensure_model_available,
    send_chat_completion,
    warm_up_model,
)
from agents.services.output_summarizer import summarize_output
from agents.services.tool_registry import dispatch_tool_call, get_all_tool_definitions
from agents.types import (
    AgentConfig,
    AgentResult,
    AgentStopReason,
    ChatMessage,
    DMRConfig,
    ToolContext,
    ToolResult,
)
from environments.types import ContainerPorts

logger = logging.getLogger(__name__)


def build_system_prompt(task_description: str) -> str:
    """Build the system prompt for the agent."""
    return (
        "You are an AI test automation agent operating inside a Linux desktop environment "
        "(Ubuntu 24.04 with XFCE4). You have three categories of tools:\n\n"
        "1. SHELL: Execute commands via SSH (apt-get, bash scripts, etc.)\n"
        "2. SCREEN: Interact with the desktop (screenshots, mouse clicks, keyboard input)\n"
        "3. BROWSER: Control the Chromium browser (navigate, click elements, type text)\n\n"
        "ENVIRONMENT:\n"
        "- The desktop (XFCE4) is already running on display :0\n"
        "- Chromium browser is ALREADY RUNNING on the desktop. Do NOT try to install or launch it.\n"
        '- To start browsing, use browser_navigate(url="...") directly.\n\n'
        "BROWSER TOOLS use natural-language descriptions, NOT CSS selectors:\n"
        '- browser_navigate(url="https://google.com") - go to a URL (use this FIRST for web tasks)\n'
        '- browser_click(description="the Login button") - describe what to click\n'
        '- browser_type(description="the username input field", text="admin") - describe the field\n'
        '- browser_hover(description="the Settings menu") - describe what to hover\n\n'
        "SCREENSHOT TOOLS require a question:\n"
        '- take_screenshot(question="What windows are open?") - asks about the desktop\n'
        '- browser_take_screenshot(question="Is the login form visible?") - asks about the browser\n\n'
        "When you have completed the task, respond with a text message summarizing what you did. "
        "Do NOT call any tools when you are done - just provide your final text response.\n\n"
        "IMPORTANT:\n"
        "- For web tasks, start with browser_navigate — the browser is already running\n"
        "- Use 'execute_command' for shell operations\n"
        "- Use screenshot tools with specific questions to observe the environment\n"
        "- Use browser tools with descriptive element names for web testing\n"
        "- Always check tool output and use screenshots to verify results\n"
        "- If a tool fails, try to diagnose and fix the issue\n\n"
        f"YOUR TASK:\n{task_description}"
    )


def build_agent_config(
    *,
    model: str | None = None,
    vision_model: str | None = None,
) -> AgentConfig:
    """Build AgentConfig from Django settings."""
    dmr_config = build_dmr_config(model=model)
    vision_config = build_vision_dmr_config(model=vision_model)
    return AgentConfig(
        dmr=dmr_config,
        vision_dmr=vision_config,
        max_iterations=settings.AGENT_MAX_ITERATIONS,
        timeout_seconds=settings.AGENT_TIMEOUT_SECONDS,
    )


def run_agent(
    task_description: str,
    ports: ContainerPorts,
    *,
    config: AgentConfig | None = None,
) -> AgentResult:
    """Run the agent loop to complete a task.

    The loop:
    1. Build system prompt + user message
    2. Call DMR with messages + tool definitions
    3. If response has tool_calls: execute each, append results
    4. If response has no tool_calls (text only): task complete
    5. Repeat until done, max_iterations, or timeout
    """
    if config is None:
        config = build_agent_config()

    ensure_model_available(config.dmr)
    if config.vision_dmr is not None:
        ensure_model_available(config.vision_dmr)

    # Warm up models so first real request doesn't hit cold-start timeout
    warm_up_model(config.dmr)
    if config.vision_dmr is not None:
        warm_up_model(config.vision_dmr)

    summarizer_config = build_summarizer_config(model=config.dmr.model)

    with AgentResourceManager(ports) as resources:
        context = ToolContext(
            ports=ports,
            ssh_session=resources.ssh,
            playwright_session=resources.playwright,
            summarizer_config=summarizer_config,
            vision_config=config.vision_dmr,
        )
        return _run_agent_loop(task_description, context, config=config)


def _run_agent_loop(
    task_description: str,
    context: ToolContext,
    *,
    config: AgentConfig,
) -> AgentResult:
    """Inner agent loop extracted for testability."""
    system_prompt = build_system_prompt(task_description)
    tool_definitions = get_all_tool_definitions()

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=task_description),
    ]

    start_time = time.monotonic()
    iterations = 0

    while iterations < config.max_iterations:
        # Check timeout
        elapsed = time.monotonic() - start_time
        if elapsed > config.timeout_seconds:
            logger.warning("Agent timed out after %.1f seconds", elapsed)
            return AgentResult(
                stop_reason=AgentStopReason.TIMEOUT,
                iterations=iterations,
                messages=tuple(messages),
                error=f"Timed out after {elapsed:.1f} seconds",
            )

        iterations += 1
        logger.info("Agent iteration %d/%d", iterations, config.max_iterations)

        try:
            response = send_chat_completion(
                config.dmr,
                tuple(messages),
                tool_definitions,
            )
        except Exception as e:
            logger.error("DMR request failed: %s", e)
            return AgentResult(
                stop_reason=AgentStopReason.ERROR,
                iterations=iterations,
                messages=tuple(messages),
                error=f"DMR request failed: {e}",
            )

        # Log agent thinking and response
        if response.reasoning_content:
            logger.info("[Thinking] %s", response.reasoning_content)
        if isinstance(response.message.content, str) and response.message.content:
            logger.info("[Agent] %s", response.message.content)

        # Add assistant message to history
        messages.append(response.message)

        # If no tool calls, the agent is done
        if response.message.tool_calls is None:
            logger.info("Agent completed task after %d iterations", iterations)
            return AgentResult(
                stop_reason=AgentStopReason.TASK_COMPLETE,
                iterations=iterations,
                messages=tuple(messages),
            )

        # Execute tool calls
        for tool_call in response.message.tool_calls:
            logger.info("[Tool Call] %s(%s)", tool_call.tool_name, tool_call.arguments)
            tool_result = dispatch_tool_call(tool_call, context)

            tool_message = _build_tool_result_message(
                tool_result,
                summarizer_config=context.summarizer_config,
            )
            logger.info("[Tool Result] %s", tool_message.content)
            messages.append(tool_message)

    logger.warning("Agent hit max iterations: %d", config.max_iterations)
    return AgentResult(
        stop_reason=AgentStopReason.MAX_ITERATIONS,
        iterations=iterations,
        messages=tuple(messages),
    )


def _build_tool_result_message(
    tool_result: ToolResult,
    *,
    summarizer_config: DMRConfig | None = None,
) -> ChatMessage:
    """Build a ChatMessage from a ToolResult, summarizing large outputs."""
    content = summarize_output(
        tool_result.content,
        tool_name=tool_result.tool_call_id,
        is_error=tool_result.is_error,
        summarizer_config=summarizer_config,
    )
    return ChatMessage(
        role="tool",
        content=content,
        tool_call_id=tool_result.tool_call_id,
    )
