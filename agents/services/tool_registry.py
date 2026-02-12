from __future__ import annotations

import logging
from typing import Callable, cast

from agents.services import tools_browser, tools_screen, tools_shell, tools_vnc
from agents.services.tool_definitions import (
    get_all_tool_definitions as get_all_tool_definitions,
)
from agents.types import (
    ToolCall,
    ToolContext,
    ToolResult,
)

logger = logging.getLogger(__name__)

_HandlerFunc = Callable[[ToolContext, dict[str, object]], ToolResult]


def dispatch_tool_call(tool_call: ToolCall, context: ToolContext) -> ToolResult:
    handler = _TOOL_HANDLERS.get(tool_call.tool_name)
    if handler is None:
        return ToolResult(
            tool_call_id=tool_call.tool_call_id,
            content=f"Unknown tool: {tool_call.tool_name}",
            is_error=True,
        )

    result = handler(context, tool_call.arguments)
    return ToolResult(
        tool_call_id=tool_call.tool_call_id,
        content=result.content,
        is_error=result.is_error,
    )


def _handle_execute_command(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    command = str(arguments.get("command", ""))
    return tools_shell.execute_command(context.ssh_session, command=command)


def _handle_take_screenshot(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    question = str(arguments.get("question", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_screen.take_screenshot(
        context.ssh_session,
        question=question,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


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
    return tools_browser.browser_navigate(
        context.playwright_session,
        url=url,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_browser_click(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_browser.browser_click(
        context.playwright_session,
        description=description,
        dmr_config=context.vision_config,
    )


def _handle_browser_type(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    description = str(arguments.get("description", ""))
    text = str(arguments.get("text", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_browser.browser_type(
        context.playwright_session,
        description=description,
        text=text,
        dmr_config=context.vision_config,
    )


def _handle_browser_hover(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_browser.browser_hover(
        context.playwright_session,
        description=description,
        dmr_config=context.vision_config,
    )


def _handle_browser_get_page_content(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    max_length = cast(int, arguments.get("max_length", 5000))
    return tools_browser.browser_get_page_content(
        context.playwright_session, max_length=max_length
    )


def _handle_browser_get_url(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_browser.browser_get_url(context.playwright_session)


def _handle_browser_take_screenshot(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    question = str(arguments.get("question", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_browser.browser_take_screenshot(
        context.playwright_session,
        question=question,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_vnc_take_screenshot(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    question = str(arguments.get("question", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_vnc.vnc_take_screenshot(
        context.vnc_session,
        question=question,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_vnc_click(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_vnc.vnc_click(
        context.vnc_session,
        description=description,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_vnc_type(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    description = str(arguments.get("description", ""))
    text = str(arguments.get("text", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_vnc.vnc_type(
        context.vnc_session,
        description=description,
        text=text,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_vnc_hover(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_vnc.vnc_hover(
        context.vnc_session,
        description=description,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_vnc_key_press(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    keys = str(arguments.get("keys", ""))
    return tools_vnc.vnc_key_press(context.vnc_session, keys=keys)


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
    "browser_hover": _handle_browser_hover,
    "browser_get_page_content": _handle_browser_get_page_content,
    "browser_get_url": _handle_browser_get_url,
    "browser_take_screenshot": _handle_browser_take_screenshot,
    "vnc_take_screenshot": _handle_vnc_take_screenshot,
    "vnc_click": _handle_vnc_click,
    "vnc_type": _handle_vnc_type,
    "vnc_hover": _handle_vnc_hover,
    "vnc_key_press": _handle_vnc_key_press,
}
