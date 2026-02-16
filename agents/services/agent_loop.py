from __future__ import annotations

import logging
import time
from collections.abc import Callable

from django.conf import settings

from agents.services.context_summarizer import summarize_context_if_needed
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

logger = logging.getLogger(__name__)


def _debug_log(message: str, on_log: Callable[[str], None] | None) -> None:
    logger.debug(message)
    if on_log is not None:
        on_log(message)


def build_system_prompt(task_description: str) -> str:
    return "\n\n".join(
        [
            _build_role_description(),
            _build_tool_guidelines(),
            _build_task_section(task_description),
        ]
    )


def _build_role_description() -> str:
    return "\n\n".join(
        [
            _build_agent_persona(),
            _build_qa_rules(),
            _build_tool_taxonomy(),
            _build_environment_context(),
        ]
    )


def _build_agent_persona() -> str:
    return (
        "You are a strict QA tester operating inside a Linux desktop environment "
        "(Ubuntu 24.04 with XFCE4). Your job is to execute test cases exactly as written "
        "and report honest results."
    )


def _build_qa_rules() -> str:
    return (
        "CRITICAL RULES:\n"
        "- You MUST follow each test step literally. If a step is ambiguous, incomplete, "
        "or impossible to execute, you MUST FAIL the test — do NOT guess, simulate, or skip.\n"
        "- If you cannot access a resource, find a UI element, or perform an action described "
        "in the test steps, FAIL the test immediately with a clear explanation of what went wrong.\n"
        "- NEVER pretend a step succeeded. NEVER fabricate results. If something does not work "
        "as described, the test FAILS.\n"
        "- Verify every expected result. If the actual result does not match the expected result, "
        "FAIL the test."
    )


def _build_tool_taxonomy() -> str:
    return (
        "You have the following tools:\n\n"
        "DESKTOP TOOLS (PyAutoGUI — for desktop/native app interactions):\n"
        "1. execute_command — Run shell commands in the container\n"
        "2. take_screenshot — Capture the desktop and answer a question about it using vision AI\n"
        "3. click — Click an element found by vision-based natural-language description\n"
        "4. type_text — Type text using the keyboard\n"
        "5. key_press — Press a key or key combination\n"
        "6. hover — Hover over an element found by vision-based description\n"
        "7. drag — Drag from one element to another, both found by vision-based description\n\n"
        "BROWSER TOOLS (Playwright — for web page interactions):\n"
        "8. browser_navigate — Navigate the browser to a URL\n"
        "9. browser_click — Click a web page element found by AI-based description\n"
        "10. browser_type — Type text into a web page element found by AI-based description\n"
        "11. browser_hover — Hover over a web page element found by AI-based description\n"
        "12. browser_get_page_content — Get the text content of the current page\n"
        "13. browser_get_url — Get the current browser URL\n"
        "14. browser_take_screenshot — Take a browser screenshot and answer a question about it"
    )


def _build_environment_context() -> str:
    return (
        "ENVIRONMENT:\n"
        "- The desktop (XFCE4) is already running on display :0\n"
        "- Chromium browser is available via browser tools (Playwright). "
        "Use browser_navigate to open a URL — no need to launch a browser manually.\n"
        "- Before running privileged commands, check your user with `whoami` and "
        "whether `sudo` is available. Adapt your commands accordingly."
    )


def _build_tool_guidelines() -> str:
    return (
        "TOOL USAGE:\n\n"
        "DESKTOP TOOLS (for native desktop interactions):\n"
        "- Vision-based tools (click, hover, drag) use natural-language descriptions "
        "to find elements on the screen via AI vision.\n"
        '- click(description="the Login button") — describe what to click\n'
        '- hover(description="the Settings menu") — describe what to hover\n'
        '- drag(start_description="file icon", end_description="trash icon") — describe start and end\n'
        '- type_text(text="hello") — type text using the keyboard\n'
        '- key_press(keys="Return") — press a key or key combination\n'
        '- take_screenshot(question="What is on screen?") — capture desktop and ask about it\n'
        '- execute_command(command="ls -la") — run a shell command\n\n'
        "BROWSER TOOLS (for web page interactions — preferred for web testing):\n"
        '- browser_navigate(url="https://example.com") — open a URL in the browser\n'
        '- browser_click(description="the Login button") — click a web page element\n'
        '- browser_type(description="the email input", text="user@example.com") — type into a web element\n'
        '- browser_hover(description="the Settings menu") — hover over a web element\n'
        "- browser_get_page_content() — get the visible text of the current page\n"
        "- browser_get_url() — get the current page URL\n"
        '- browser_take_screenshot(question="What does the page show?") — capture browser and ask about it\n\n'
        "WHEN TO USE WHICH:\n"
        "- For web testing, prefer browser_* tools. They are faster and more reliable than "
        "desktop tools for web interactions.\n"
        "- Use desktop tools (click, type_text, key_press) for native desktop applications.\n"
        "- Use execute_command for shell operations.\n\n"
        "SHELL RULES:\n"
        "- If you launch a GUI application or long-running process (e.g. gnome-calculator, "
        "flask run, node server.js, vim), append ' &' so it runs in the "
        "background. Otherwise the command will block and time out.\n\n"
        "When you have completed the task, respond with a text message summarizing what you did. "
        "Do NOT call any tools when you are done - just provide your final text response."
    )


def _build_task_section(task_description: str) -> str:
    return (
        "IMPORTANT:\n"
        "- For web tasks, use browser_navigate to open URLs — no need to launch a browser manually\n"
        "- Use browser_click/browser_type for web page interactions\n"
        "- Use 'execute_command' for shell operations\n"
        "- Use take_screenshot or browser_take_screenshot with specific questions to observe the environment\n"
        "- Use click/hover with descriptive element names for native desktop GUI interactions\n"
        "- Always check tool output and use screenshots to verify results\n"
        "- If a tool fails, try to diagnose and fix the issue\n\n"
        f"YOUR TASK:\n{task_description}"
    )


def build_agent_config(
    *,
    model: str | None = None,
    vision_model: str | None = None,
) -> AgentConfig:
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
    project_id: int,
    *,
    config: AgentConfig | None = None,
) -> AgentResult:
    if config is None:
        config = build_agent_config()

    ensure_model_available(config.dmr)
    if config.vision_dmr is not None and config.vision_dmr.api_key is None:
        ensure_model_available(config.vision_dmr)

    warm_up_model(config.dmr)
    if config.vision_dmr is not None and config.vision_dmr.api_key is None:
        warm_up_model(config.vision_dmr)

    summarizer_config = build_summarizer_config()

    context = ToolContext(
        project_id=project_id,
        summarizer_config=summarizer_config,
        vision_config=config.vision_dmr,
        on_screenshot=config.on_screenshot,
    )
    return _run_agent_loop(task_description, context, config=config)


def _run_agent_loop(
    task_description: str,
    context: ToolContext,
    *,
    config: AgentConfig,
) -> AgentResult:
    system_prompt = build_system_prompt(task_description)
    tool_definitions = get_all_tool_definitions()

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=task_description),
    ]

    start_time = time.monotonic()
    iterations = 0
    on_log = config.on_log

    while iterations < config.max_iterations:
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
        _debug_log(f"Agent iteration {iterations}/{config.max_iterations}", on_log)

        messages = summarize_context_if_needed(
            messages,
            summarizer_config=context.summarizer_config,
        )

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

        if response.reasoning_content:
            _debug_log(f"[Thinking] {response.reasoning_content}", on_log)
        if isinstance(response.message.content, str) and response.message.content:
            _debug_log(f"[Agent] {response.message.content}", on_log)

        messages.append(response.message)

        if response.message.tool_calls is None:
            _debug_log(f"Agent completed task after {iterations} iterations", on_log)
            return AgentResult(
                stop_reason=AgentStopReason.TASK_COMPLETE,
                iterations=iterations,
                messages=tuple(messages),
            )

        for tool_call in response.message.tool_calls:
            _debug_log(
                f"[Tool Call] {tool_call.tool_name}({tool_call.arguments})", on_log
            )
            tool_result = dispatch_tool_call(tool_call, context)

            tool_message = _build_tool_result_message(
                tool_result,
                summarizer_config=context.summarizer_config,
            )
            _debug_log(f"[Tool Result] {tool_message.content}", on_log)
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
