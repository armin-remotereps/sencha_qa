from __future__ import annotations

import logging
from typing import Callable, cast

from agents.services import tools_browser, tools_screen, tools_shell
from agents.types import (
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    ToolParameter,
    ToolResult,
)

logger = logging.getLogger(__name__)


def get_all_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Return all available tool definitions.

    Returns:
        Tuple of all tool definitions for shell, screen, and browser categories
    """
    return (
        # Shell tools
        ToolDefinition(
            name="execute_command",
            description="Execute a shell command in the container via SSH. Returns stdout, stderr, and exit code.",
            category=ToolCategory.SHELL,
            parameters=(
                ToolParameter(
                    name="command",
                    type="string",
                    description="The shell command to execute.",
                    required=True,
                ),
            ),
        ),
        # Screen tools
        ToolDefinition(
            name="take_screenshot",
            description="Take a screenshot of the desktop. Returns the screenshot as an image.",
            category=ToolCategory.SCREEN,
            parameters=(),
        ),
        ToolDefinition(
            name="screen_click",
            description="Click at a specific position on the desktop.",
            category=ToolCategory.SCREEN,
            parameters=(
                ToolParameter(
                    name="x",
                    type="integer",
                    description="X coordinate to click.",
                    required=True,
                ),
                ToolParameter(
                    name="y",
                    type="integer",
                    description="Y coordinate to click.",
                    required=True,
                ),
                ToolParameter(
                    name="button",
                    type="integer",
                    description="Mouse button (1=left, 2=middle, 3=right).",
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="screen_type_text",
            description="Type text on the desktop using the keyboard.",
            category=ToolCategory.SCREEN,
            parameters=(
                ToolParameter(
                    name="text",
                    type="string",
                    description="Text to type.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="screen_key_press",
            description="Press a key or key combination (e.g., 'Return', 'ctrl+c', 'alt+F4').",
            category=ToolCategory.SCREEN,
            parameters=(
                ToolParameter(
                    name="keys",
                    type="string",
                    description="Key or key combination to press.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="screen_list_windows",
            description="List all open windows on the desktop.",
            category=ToolCategory.SCREEN,
            parameters=(),
        ),
        ToolDefinition(
            name="screen_get_active_window",
            description="Get the name of the currently active window.",
            category=ToolCategory.SCREEN,
            parameters=(),
        ),
        # Browser tools
        ToolDefinition(
            name="browser_navigate",
            description="Navigate to a URL in the browser.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="url",
                    type="string",
                    description="URL to navigate to.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_click",
            description="Click an element in the browser by CSS selector.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="selector",
                    type="string",
                    description="CSS selector of the element to click.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_type",
            description="Type text into a form element in the browser.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="selector",
                    type="string",
                    description="CSS selector of the element.",
                    required=True,
                ),
                ToolParameter(
                    name="text",
                    type="string",
                    description="Text to type.",
                    required=True,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_get_page_content",
            description="Get the text content of the current browser page.",
            category=ToolCategory.BROWSER,
            parameters=(
                ToolParameter(
                    name="max_length",
                    type="integer",
                    description="Maximum length of content to return.",
                    required=False,
                ),
            ),
        ),
        ToolDefinition(
            name="browser_get_url",
            description="Get the current URL of the browser.",
            category=ToolCategory.BROWSER,
            parameters=(),
        ),
        ToolDefinition(
            name="browser_take_screenshot",
            description="Take a screenshot of the browser viewport. Returns the screenshot as an image.",
            category=ToolCategory.BROWSER,
            parameters=(),
        ),
    )


def dispatch_tool_call(tool_call: ToolCall, context: ToolContext) -> ToolResult:
    """Dispatch a tool call to the appropriate handler function.

    Args:
        tool_call: The tool call with name and arguments
        context: Tool execution context with ports, SSH session, etc.

    Returns:
        ToolResult with the result of the tool execution
    """
    handler = _TOOL_HANDLERS.get(tool_call.tool_name)
    if handler is None:
        return ToolResult(
            tool_call_id=tool_call.tool_call_id,
            content=f"Unknown tool: {tool_call.tool_name}",
            is_error=True,
        )

    result = handler(context, tool_call.arguments)
    # Set the correct tool_call_id on the result
    return ToolResult(
        tool_call_id=tool_call.tool_call_id,
        content=result.content,
        is_error=result.is_error,
        image_base64=result.image_base64,
    )


# ============================================================================
# PRIVATE HANDLER WRAPPERS
# ============================================================================
# These extract typed arguments from the dict and call the actual tool functions


def _handle_execute_command(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    command = str(arguments.get("command", ""))
    return tools_shell.execute_command(context.ssh_session, command=command)


def _handle_take_screenshot(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_screen.take_screenshot(context.ssh_session)


def _handle_screen_click(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    x = cast(int, arguments.get("x", 0))
    y = cast(int, arguments.get("y", 0))
    button = cast(int, arguments.get("button", 1))
    return tools_screen.screen_click(context.ssh_session, x=x, y=y, button=button)


def _handle_screen_type_text(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    text = str(arguments.get("text", ""))
    return tools_screen.screen_type_text(context.ssh_session, text=text)


def _handle_screen_key_press(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    keys = str(arguments.get("keys", ""))
    return tools_screen.screen_key_press(context.ssh_session, keys=keys)


def _handle_screen_list_windows(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_screen.screen_list_windows(context.ssh_session)


def _handle_screen_get_active_window(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_screen.screen_get_active_window(context.ssh_session)


def _handle_browser_navigate(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    url = str(arguments.get("url", ""))
    return tools_browser.browser_navigate(context.ports, url=url)


def _handle_browser_click(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    selector = str(arguments.get("selector", ""))
    return tools_browser.browser_click(context.ports, selector=selector)


def _handle_browser_type(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    selector = str(arguments.get("selector", ""))
    text = str(arguments.get("text", ""))
    return tools_browser.browser_type(context.ports, selector=selector, text=text)


def _handle_browser_get_page_content(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    max_length = cast(int, arguments.get("max_length", 5000))
    return tools_browser.browser_get_page_content(context.ports, max_length=max_length)


def _handle_browser_get_url(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_browser.browser_get_url(context.ports)


def _handle_browser_take_screenshot(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_browser.browser_take_screenshot(context.ports)


# Handler type alias
_HandlerFunc = Callable[[ToolContext, dict[str, object]], ToolResult]

_TOOL_HANDLERS: dict[str, _HandlerFunc] = {
    "execute_command": _handle_execute_command,
    "take_screenshot": _handle_take_screenshot,
    "screen_click": _handle_screen_click,
    "screen_type_text": _handle_screen_type_text,
    "screen_key_press": _handle_screen_key_press,
    "screen_list_windows": _handle_screen_list_windows,
    "screen_get_active_window": _handle_screen_get_active_window,
    "browser_navigate": _handle_browser_navigate,
    "browser_click": _handle_browser_click,
    "browser_type": _handle_browser_type,
    "browser_get_page_content": _handle_browser_get_page_content,
    "browser_get_url": _handle_browser_get_url,
    "browser_take_screenshot": _handle_browser_take_screenshot,
}
