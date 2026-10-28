from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable

from agents.services import tools_controller, tools_search, tools_utility
from agents.services.tool_definitions import (
    get_all_tool_definitions as get_all_tool_definitions,
)
from agents.types import (
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolDispatcher,
    ToolResult,
)
from controller_client.protocol import (
    DEFAULT_SCROLL_AMOUNT,
    MAX_SCROLL_AMOUNT,
    BrowserName,
    ScrollDirection,
)

logger = logging.getLogger(__name__)

_HandlerFunc = Callable[[ToolContext, dict[str, object]], ToolResult]


class _InvalidArgumentError(ValueError):
    """A tool argument broke its rule; the message names the rule."""


def _invalid_argument_result(error: _InvalidArgumentError) -> ToolResult:
    return ToolResult(tool_call_id="", content=str(error), is_error=True)


def _strict_int(name: str, raw: object) -> int:
    """Read a JSON integer: an int, or a float with no fraction; never bool or str."""
    if isinstance(raw, bool):
        raise _InvalidArgumentError(f"{name} must be an integer, not a boolean.")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    raise _InvalidArgumentError(f"{name} must be an integer, got {raw!r}.")


def _read_point(arguments: dict[str, object]) -> tuple[int | None, int | None]:
    """Read the optional x/y pair of screenshot pixel coordinates (both or neither)."""
    raw_x, raw_y = arguments.get("x"), arguments.get("y")
    if raw_x is None and raw_y is None:
        return None, None
    if raw_x is None or raw_y is None:
        raise _InvalidArgumentError("x and y must be given together, or both omitted.")
    x, y = _strict_int("x", raw_x), _strict_int("y", raw_y)
    if x < 0 or y < 0:
        raise _InvalidArgumentError(
            "x and y are screenshot pixel coordinates and cannot be negative."
        )
    return x, y


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


def tool_definitions_named(names: frozenset[str]) -> tuple[ToolDefinition, ...]:
    return tuple(
        definition
        for definition in get_all_tool_definitions()
        if definition.name in names
    )


def allowlisted_dispatcher(names: frozenset[str]) -> ToolDispatcher:
    """``dispatch_tool_call`` refusing every tool outside ``names``.

    Enforced here, not only by the definitions the model is shown.
    """

    def _dispatch(tool_call: ToolCall, context: ToolContext) -> ToolResult:
        if tool_call.tool_name not in names:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                content=f"Tool not available in this task: {tool_call.tool_name}",
                is_error=True,
            )
        return dispatch_tool_call(tool_call, context)

    return _dispatch


def _handle_execute_command(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    command = str(arguments.get("command", ""))
    raw_cwd = arguments.get("cwd")
    cwd = str(raw_cwd) if raw_cwd else ""
    return tools_controller.execute_command(
        context.project_id, command=command, cwd=cwd, on_log=context.on_log
    )


def _handle_send_command_input(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    session_id = str(arguments.get("session_id", ""))
    input_text = str(arguments.get("input_text", ""))
    return tools_controller.send_command_input(
        context.project_id, session_id=session_id, input_text=input_text
    )


def _handle_wait_for_command(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    session_id = str(arguments.get("session_id", ""))
    return tools_controller.wait_for_command(
        context.project_id, session_id=session_id, on_log=context.on_log
    )


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


def _read_expected_result(arguments: dict[str, object]) -> str:
    raw_expected_result = arguments.get("expected_result")
    return str(raw_expected_result) if raw_expected_result else ""


def _handle_click(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.click(
        context.project_id,
        description=description,
        expected_result=_read_expected_result(arguments),
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
        cancellation_check=context.cancellation_check,
        deadline=context.deadline,
    )


def _handle_double_click(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.double_click(
        context.project_id,
        description=description,
        expected_result=_read_expected_result(arguments),
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
        cancellation_check=context.cancellation_check,
        deadline=context.deadline,
    )


def _handle_click_at(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    try:
        x, y = _read_point(arguments)
    except _InvalidArgumentError as error:
        return _invalid_argument_result(error)
    if x is None or y is None:
        return ToolResult(
            tool_call_id="",
            content="click_at needs both x and y (screenshot pixel coordinates).",
            is_error=True,
        )
    return tools_controller.click_at(
        context.project_id,
        x=x,
        y=y,
        expected_result=_read_expected_result(arguments),
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
        cancellation_check=context.cancellation_check,
        deadline=context.deadline,
    )


def _handle_scroll(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    try:
        direction = ScrollDirection(str(arguments.get("direction", "")))
    except ValueError:
        allowed = ", ".join(d.value for d in ScrollDirection)
        return ToolResult(
            tool_call_id="",
            content=f"direction must be one of: {allowed}.",
            is_error=True,
        )
    try:
        raw_amount = arguments.get("amount")
        amount = (
            DEFAULT_SCROLL_AMOUNT
            if raw_amount is None
            else _strict_int("amount", raw_amount)
        )
        if not 1 <= amount <= MAX_SCROLL_AMOUNT:
            raise _InvalidArgumentError(
                f"amount must be between 1 and {MAX_SCROLL_AMOUNT} wheel notches."
            )
        x, y = _read_point(arguments)
    except _InvalidArgumentError as error:
        return _invalid_argument_result(error)
    return tools_controller.scroll(
        context.project_id, direction=direction, amount=amount, x=x, y=y
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
        expected_result=_read_expected_result(arguments),
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
        cancellation_check=context.cancellation_check,
        deadline=context.deadline,
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
        expected_result=_read_expected_result(arguments),
        vision_config=context.vision_config,
        on_screenshot=context.on_screenshot,
        cancellation_check=context.cancellation_check,
        deadline=context.deadline,
    )


def _handle_launch_app(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    app_name = str(arguments.get("app_name", ""))
    return tools_controller.launch_app(context.project_id, app_name=app_name)


def _handle_close_app(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    app_name = str(arguments.get("app_name", ""))
    # Strict bool: a force-kill discards unsaved work, so only a real JSON true
    # counts; strings such as "false" must not turn into a forced close.
    force = arguments.get("force") is True
    return tools_controller.close_app(
        context.project_id, app_name=app_name, force=force
    )


def _handle_close_tab(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    app_name = str(arguments.get("app_name", ""))
    return tools_controller.close_tab(context.project_id, app_name=app_name)


def _handle_check_app_installed(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    app_name = str(arguments.get("app_name", ""))
    return tools_controller.check_app_installed(context.project_id, app_name=app_name)


def _handle_browser_navigate(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    url = str(arguments.get("url", ""))
    raw_browser = arguments.get("browser")
    browser: BrowserName | None = None
    if raw_browser is not None and raw_browser != "":
        try:
            browser = BrowserName(str(raw_browser))
        except ValueError:
            allowed = ", ".join(b.value for b in BrowserName)
            return ToolResult(
                tool_call_id="",
                content=f"Invalid browser '{raw_browser}'. Allowed values: {allowed}.",
                is_error=True,
            )
    return tools_controller.browser_navigate(
        context.project_id,
        url=url,
        browser=browser,
        selection=context.browser_selection,
    )


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
        selection=context.browser_selection,
        vision_config=context.vision_config,
    )


def _handle_browser_double_click(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    description = str(arguments.get("description", ""))
    if context.vision_config is None:
        return ToolResult(
            tool_call_id="", content="Vision model not configured.", is_error=True
        )
    return tools_controller.browser_double_click(
        context.project_id,
        description=description,
        selection=context.browser_selection,
        vision_config=context.vision_config,
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
        selection=context.browser_selection,
        text=text,
        vision_config=context.vision_config,
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
        selection=context.browser_selection,
        vision_config=context.vision_config,
    )


def _handle_browser_get_page_content(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_controller.browser_get_page_content(
        context.project_id, selection=context.browser_selection
    )


def _handle_browser_get_url(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_controller.browser_get_url(
        context.project_id, selection=context.browser_selection
    )


def _handle_browser_download(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    url = str(arguments.get("url", ""))
    save_path = str(arguments.get("save_path", ""))
    return tools_controller.browser_download(
        context.project_id,
        url=url,
        save_path=save_path,
        selection=context.browser_selection,
    )


def _handle_browser_list_downloads(
    context: ToolContext, arguments: dict[str, object]
) -> ToolResult:
    return tools_controller.browser_list_downloads(context.project_id)


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
        selection=context.browser_selection,
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
    return tools_search.web_search(
        query=raw_query,
        summarizer_config=context.summarizer_config,
    )


def _handle_wait(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    return tools_utility.wait(
        arguments.get("seconds"),
        cancellation_check=context.cancellation_check,
        deadline=context.deadline,
    )


_TOOL_HANDLERS: dict[str, _HandlerFunc] = {
    "execute_command": _handle_execute_command,
    "send_command_input": _handle_send_command_input,
    "wait_for_command": _handle_wait_for_command,
    "take_screenshot": _handle_take_screenshot,
    "click": _handle_click,
    "double_click": _handle_double_click,
    "click_at": _handle_click_at,
    "scroll": _handle_scroll,
    "type_text": _handle_type_text,
    "key_press": _handle_key_press,
    "hover": _handle_hover,
    "drag": _handle_drag,
    "launch_app": _handle_launch_app,
    "close_app": _handle_close_app,
    "close_tab": _handle_close_tab,
    "check_app_installed": _handle_check_app_installed,
    "browser_navigate": _handle_browser_navigate,
    "browser_click": _handle_browser_click,
    "browser_double_click": _handle_browser_double_click,
    "browser_type": _handle_browser_type,
    "browser_hover": _handle_browser_hover,
    "browser_get_page_content": _handle_browser_get_page_content,
    "browser_get_url": _handle_browser_get_url,
    "browser_take_screenshot": _handle_browser_take_screenshot,
    "browser_download": _handle_browser_download,
    "browser_list_downloads": _handle_browser_list_downloads,
    "web_search": _handle_web_search,
    "wait": _handle_wait,
}
