from __future__ import annotations

import dataclasses
import logging
from typing import Callable

from agents.services import tools_controller, tools_search
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
    return dataclasses.replace(result, tool_call_id=tool_call.tool_call_id)


def _handle_execute_command(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    command = str(arguments.get("command", ""))
    return tools_controller.execute_command(context.project_id, command=command)


def _handle_take_screenshot(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    question = str(arguments.get("question", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.take_screenshot(
        context.project_id,
        question=question,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_click(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.click(
        context.project_id,
        description=description,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_type_text(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    text = str(arguments.get("text", ""))
    return tools_controller.type_text(context.project_id, text=text)


def _handle_key_press(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    keys = str(arguments.get("keys", ""))
    return tools_controller.key_press(context.project_id, keys=keys)


def _handle_hover(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.hover(
        context.project_id,
        description=description,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_drag(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    start_description = str(arguments.get("start_description", ""))
    end_description = str(arguments.get("end_description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.drag(
        context.project_id,
        start_description=start_description,
        end_description=end_description,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_browser_navigate(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    url = str(arguments.get("url", ""))
    return tools_controller.browser_navigate(context.project_id, url=url)


def _handle_browser_click(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.browser_click(
        context.project_id,
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
    return tools_controller.browser_type(
        context.project_id,
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
    return tools_controller.browser_hover(
        context.project_id,
        description=description,
        dmr_config=context.vision_config,
    )


def _handle_browser_get_page_content(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_controller.browser_get_page_content(context.project_id)


def _handle_browser_get_url(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_controller.browser_get_url(context.project_id)


def _handle_browser_download(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    url = str(arguments.get("url", ""))
    save_path = str(arguments.get("save_path", ""))
    return tools_controller.browser_download(
        context.project_id, url=url, save_path=save_path
    )


def _handle_browser_take_screenshot(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    question = str(arguments.get("question", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.browser_take_screenshot(
        context.project_id,
        question=question,
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
    )


def _handle_web_search(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    raw_query = arguments.get("query", "")
    if not isinstance(raw_query, str):
        return ToolResult(
            tool_call_id="",
            content=f"Invalid query type: expected string, got {type(raw_query).__name__}",
            is_error=True,
        )
    return tools_search.web_search(query=raw_query)


_TOOL_HANDLERS: dict[str, _HandlerFunc] = {
    "execute_command": _handle_execute_command,
    "take_screenshot": _handle_take_screenshot,
    "click": _handle_click,
    "type_text": _handle_type_text,
    "key_press": _handle_key_press,
    "hover": _handle_hover,
    "drag": _handle_drag,
    "browser_navigate": _handle_browser_navigate,
    "browser_click": _handle_browser_click,
    "browser_type": _handle_browser_type,
    "browser_hover": _handle_browser_hover,
    "browser_get_page_content": _handle_browser_get_page_content,
    "browser_get_url": _handle_browser_get_url,
    "browser_take_screenshot": _handle_browser_take_screenshot,
    "browser_download": _handle_browser_download,
    "web_search": _handle_web_search,
}
