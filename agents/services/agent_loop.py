from __future__ import annotations

import logging
import time

from django.conf import settings

from agents.services.agent_resource_manager import AgentResourceManager
from agents.services.dmr_client import send_chat_completion
from agents.services.dmr_config import (
    build_dmr_config,
    build_summarizer_config,
    build_vision_config,
)
from agents.services.dmr_model_manager import ensure_model_available, warm_up_model
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
    return "\n\n".join(
        [
            _build_role_description(),
            _build_tool_guidelines(),
            _build_task_section(task_description),
        ]
    )


def _build_role_description() -> str:
    return (
        "You are an AI test automation agent operating inside a Linux desktop environment "
        "(Ubuntu 24.04 with XFCE4). You have four categories of tools:\n\n"
        "1. SHELL: Execute commands via SSH (apt-get, bash scripts, etc.)\n"
        "2. SCREEN: Interact with the desktop via SSH+xdotool (coordinate-based screenshots, mouse, keyboard)\n"
        "3. BROWSER: Control the Chromium browser via Playwright CDP (DOM-based element finding)\n"
        "4. VNC: Interact with the desktop via VNC protocol (vision-based element finding)\n\n"
        "ENVIRONMENT:\n"
        "- The desktop (XFCE4) is already running on display :0\n"
        "- Chromium browser is ALREADY RUNNING on the desktop. Do NOT try to install or launch it.\n"
        '- To start browsing, use browser_navigate(url="...") directly.\n'
        "- You are running as root. Do NOT use sudo — it is not installed."
    )


def _build_tool_guidelines() -> str:
    return (
        "BROWSER TOOLS use natural-language descriptions, NOT CSS selectors:\n"
        '- browser_navigate(url="https://google.com") - go to a URL (use this FIRST for web tasks)\n'
        '- browser_click(description="the Login button") - describe what to click\n'
        '- browser_type(description="the username input field", text="admin") - describe the field\n'
        '- browser_hover(description="the Settings menu") - describe what to hover\n\n'
        "VNC TOOLS use vision-based element finding (no DOM access):\n"
        '- vnc_take_screenshot(question="What is on screen?") - capture VNC framebuffer and ask about it\n'
        '- vnc_click(description="the OK button") - vision AI finds and clicks the element\n'
        '- vnc_type(description="the search box", text="hello") - vision AI finds input, clicks, types\n'
        '- vnc_hover(description="the File menu") - vision AI finds and hovers over element\n'
        '- vnc_key_press(keys="Return") - send key via VNC (X11 keysym names)\n\n'
        "SCREENSHOT TOOLS require a question:\n"
        '- take_screenshot(question="What windows are open?") - asks about the desktop (via SSH)\n'
        '- browser_take_screenshot(question="Is the login form visible?") - asks about the browser\n'
        '- vnc_take_screenshot(question="What dialog is showing?") - asks about VNC framebuffer\n\n'
        "SHELL RULES:\n"
        "- If you launch a GUI application or long-running process (e.g. gnome-calculator, "
        "firefox, flask run, node server.js, vim), append ' &' so it runs in the "
        "background. Otherwise the command will block for 120s and time out.\n"
        "- Example: 'gnome-calculator &' instead of 'gnome-calculator'\n\n"
        "When you have completed the task, respond with a text message summarizing what you did. "
        "Do NOT call any tools when you are done - just provide your final text response."
    )


def _build_task_section(task_description: str) -> str:
    return (
        "IMPORTANT:\n"
        "- For web tasks, start with browser_navigate — the browser is already running\n"
        "- Use 'execute_command' for shell operations\n"
        "- Use screenshot tools with specific questions to observe the environment\n"
        "- Use browser tools with descriptive element names for web testing\n"
        "- Use VNC tools for desktop GUI interactions that need vision-based element finding\n"
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
    vision_config = build_vision_config(model=vision_model)
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
    """Execute an AI agent to complete a task in a containerized environment.

    The agent operates autonomously using provided tools until task completion,
    timeout, or iteration limit.
    """
    if config is None:
        config = build_agent_config()

    ensure_model_available(config.dmr)
    if config.vision_dmr is not None and config.vision_dmr.api_key is None:
        ensure_model_available(config.vision_dmr)

    # Warm up models so first real request doesn't hit cold-start timeout
    warm_up_model(config.dmr)
    if config.vision_dmr is not None and config.vision_dmr.api_key is None:
        warm_up_model(config.vision_dmr)

    summarizer_config = build_summarizer_config(model=config.dmr.model)

    with AgentResourceManager(ports) as resources:
        context = ToolContext(
            ports=ports,
            ssh_session=resources.ssh,
            playwright_session=resources.playwright,
            vnc_session=resources.vnc,
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
