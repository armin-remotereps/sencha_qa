from __future__ import annotations

from collections.abc import Callable

from agents.services.browser_element_finder import find_element_index
from agents.services.tool_utils import safe_tool_call
from agents.services.verified_element_actions import (
    VerificationBudget,
    confirm_action,
    default_click_expectation,
    format_verified_message,
    resolve_verified_element,
    take_before_screenshot,
)
from agents.services.vision_qa import answer_screenshot_question
from agents.types import (
    CancellationCheck,
    LLMConfig,
    LogCallback,
    PixelUIElement,
    ScreenshotCallback,
    ToolResult,
)
from projects.services import (
    ActionResult,
    ControllerActionError,
    InteractiveCommandResult,
    controller_browser_click,
    controller_browser_download,
    controller_browser_get_page_content,
    controller_browser_get_url,
    controller_browser_hover,
    controller_browser_list_downloads,
    controller_browser_navigate,
    controller_browser_take_screenshot,
    controller_browser_type,
    controller_check_app_installed,
    controller_click,
    controller_drag,
    controller_hover,
    controller_key_press,
    controller_launch_app,
    controller_screenshot,
    controller_send_input,
    controller_start_interactive_command,
    controller_type_text,
    controller_wait_for_command,
)

_LOG_PREFIX = "$ "


def _ensure_action_succeeded(result: ActionResult) -> None:
    """Surface a controller-reported failure instead of claiming the action ran.

    The controller answers input actions with either an action_result carrying
    ``success`` or an error reply; both arrive here as an ActionResult, and a
    failed one (for example Windows refusing to deliver input to an elevated
    window) must reach the agent as an error, not as "Clicked ...".
    """
    if result["success"]:
        return
    raise ControllerActionError(
        result["message"] or "Controller reported the action failed"
    )


def _format_interactive_output(
    result: InteractiveCommandResult, include_session_id: bool = False
) -> str:
    parts: list[str] = []
    if include_session_id:
        parts.append(f"Session ID: {result['session_id']}")
    if result["output"]:
        parts.append(f"Output:\n{result['output']}")
    parts.append(f"Process alive: {result['is_alive']}")
    if result["exit_code"] is not None:
        parts.append(f"Exit code: {result['exit_code']}")
    return "\n".join(parts)


def execute_command(
    project_id: int,
    *,
    command: str,
    cwd: str = "",
    on_log: LogCallback | None = None,
) -> ToolResult:
    effective_command = f"cd {cwd} && {command}" if cwd else command

    def _do() -> ToolResult:
        result = controller_start_interactive_command(project_id, effective_command)
        if on_log is not None and result["output"]:
            on_log(f"{_LOG_PREFIX}{result['output'].rstrip()}")
        if not result["is_alive"]:
            parts: list[str] = []
            if result["output"]:
                parts.append(result["output"])
            parts.append(f"Exit code: {result['exit_code']}")
            return ToolResult(
                tool_call_id="",
                content="\n".join(parts),
                is_error=result["exit_code"] != 0,
            )
        content = _format_interactive_output(result, include_session_id=True)
        return ToolResult(tool_call_id="", content=content, is_error=False)

    return safe_tool_call("execute_command", _do)


def send_command_input(
    project_id: int, *, session_id: str, input_text: str
) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_send_input(project_id, session_id, input_text)
        content = _format_interactive_output(result)
        return ToolResult(tool_call_id="", content=content, is_error=False)

    return safe_tool_call("send_command_input", _do)


def wait_for_command(
    project_id: int,
    *,
    session_id: str,
    on_log: LogCallback | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_wait_for_command(project_id, session_id)
        if on_log is not None and result["output"]:
            on_log(f"{_LOG_PREFIX}{result['output'].rstrip()}")
        content = _format_interactive_output(result)
        is_error = result["exit_code"] is not None and result["exit_code"] != 0
        return ToolResult(tool_call_id="", content=content, is_error=is_error)

    return safe_tool_call("wait_for_command", _do)


def take_screenshot(
    project_id: int,
    *,
    question: str,
    vision_config: LLMConfig,
    on_screenshot: ScreenshotCallback | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_screenshot(project_id)
        image_base64 = result["image_base64"]
        if on_screenshot is not None:
            on_screenshot(image_base64, "take_screenshot")
        answer = answer_screenshot_question(vision_config, image_base64, question)
        return ToolResult(tool_call_id="", content=answer, is_error=False)

    return safe_tool_call("take_screenshot", _do)


_AttemptFunc = Callable[[VerificationBudget], ToolResult | None]


def _run_verified_action(
    operation: str,
    attempt: _AttemptFunc,
    *,
    cancellation_check: CancellationCheck | None,
    deadline: float | None,
) -> ToolResult:
    def _do() -> ToolResult:
        budget = VerificationBudget(
            deadline=deadline, cancellation_check=cancellation_check
        )
        while True:
            result = attempt(budget)
            if result is not None:
                return result

    return safe_tool_call(operation, _do)


def _confirm_or_finish(
    project_id: int,
    vision_config: LLMConfig,
    budget: VerificationBudget,
    *,
    acted_on: tuple[PixelUIElement, ...],
    before_image_base64: str,
    action_summary: str,
    expected_result: str,
    tool_name: str,
    success_message: str,
    on_screenshot: ScreenshotCallback | None,
) -> ToolResult | None:
    if expected_result:
        verdict = confirm_action(
            project_id,
            vision_config,
            budget,
            acted_on=acted_on,
            before_image_base64=before_image_base64,
            action_summary=action_summary,
            expected_result=expected_result,
            tool_name=tool_name,
            on_screenshot=on_screenshot,
        )
        if not verdict.accepted:
            return None
    return ToolResult(
        tool_call_id="",
        content=format_verified_message(success_message, budget),
        is_error=False,
    )


def _before_image_if_confirming(project_id: int, expected_result: str) -> str:
    return take_before_screenshot(project_id) if expected_result else ""


def click(
    project_id: int,
    *,
    description: str,
    vision_config: LLMConfig,
    expected_result: str = "",
    on_screenshot: ScreenshotCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
    deadline: float | None = None,
) -> ToolResult:
    effective_expectation = expected_result or default_click_expectation(description)

    def _attempt(budget: VerificationBudget) -> ToolResult | None:
        element = resolve_verified_element(
            project_id, description, vision_config, budget, on_screenshot=on_screenshot
        )
        x, y = element.center_x, element.center_y
        before_image = take_before_screenshot(project_id)
        _ensure_action_succeeded(controller_click(project_id, x, y))
        return _confirm_or_finish(
            project_id,
            vision_config,
            budget,
            acted_on=(element,),
            before_image_base64=before_image,
            action_summary=f"clicked '{description}' at ({x}, {y})",
            expected_result=effective_expectation,
            tool_name="click",
            success_message=f"Clicked element at ({x}, {y}): {description}",
            on_screenshot=on_screenshot,
        )

    return _run_verified_action(
        "click", _attempt, cancellation_check=cancellation_check, deadline=deadline
    )


def type_text(project_id: int, *, text: str) -> ToolResult:
    def _do() -> ToolResult:
        _ensure_action_succeeded(controller_type_text(project_id, text))
        return ToolResult(
            tool_call_id="", content=f"Typed text: {text}", is_error=False
        )

    return safe_tool_call("type_text", _do)


def key_press(project_id: int, *, keys: str) -> ToolResult:
    def _do() -> ToolResult:
        _ensure_action_succeeded(controller_key_press(project_id, keys))
        return ToolResult(
            tool_call_id="", content=f"Pressed keys: {keys}", is_error=False
        )

    return safe_tool_call("key_press", _do)


def hover(
    project_id: int,
    *,
    description: str,
    vision_config: LLMConfig,
    expected_result: str = "",
    on_screenshot: ScreenshotCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
    deadline: float | None = None,
) -> ToolResult:
    def _attempt(budget: VerificationBudget) -> ToolResult | None:
        element = resolve_verified_element(
            project_id, description, vision_config, budget, on_screenshot=on_screenshot
        )
        x, y = element.center_x, element.center_y
        before_image = _before_image_if_confirming(project_id, expected_result)
        _ensure_action_succeeded(controller_hover(project_id, x, y))
        return _confirm_or_finish(
            project_id,
            vision_config,
            budget,
            acted_on=(element,),
            before_image_base64=before_image,
            action_summary=f"hovered over '{description}' at ({x}, {y})",
            expected_result=expected_result,
            tool_name="hover",
            success_message=f"Hovered over element at ({x}, {y}): {description}",
            on_screenshot=on_screenshot,
        )

    return _run_verified_action(
        "hover", _attempt, cancellation_check=cancellation_check, deadline=deadline
    )


def drag(
    project_id: int,
    *,
    start_description: str,
    end_description: str,
    vision_config: LLMConfig,
    expected_result: str = "",
    on_screenshot: ScreenshotCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
    deadline: float | None = None,
) -> ToolResult:
    def _attempt(budget: VerificationBudget) -> ToolResult | None:
        start = resolve_verified_element(
            project_id,
            start_description,
            vision_config,
            budget,
            on_screenshot=on_screenshot,
        )
        end = resolve_verified_element(
            project_id,
            end_description,
            vision_config,
            budget,
            on_screenshot=on_screenshot,
        )
        sx, sy, ex, ey = start.center_x, start.center_y, end.center_x, end.center_y
        before_image = _before_image_if_confirming(project_id, expected_result)
        _ensure_action_succeeded(controller_drag(project_id, sx, sy, ex, ey))
        return _confirm_or_finish(
            project_id,
            vision_config,
            budget,
            acted_on=(start, end),
            before_image_base64=before_image,
            action_summary=(
                f"dragged '{start_description}' at ({sx}, {sy}) onto "
                f"'{end_description}' at ({ex}, {ey})"
            ),
            expected_result=expected_result,
            tool_name="drag",
            success_message=f"Dragged from ({sx}, {sy}) to ({ex}, {ey})",
            on_screenshot=on_screenshot,
        )

    return _run_verified_action(
        "drag", _attempt, cancellation_check=cancellation_check, deadline=deadline
    )


def launch_app(project_id: int, *, app_name: str) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_launch_app(project_id, app_name)
        return ToolResult(
            tool_call_id="",
            content=result["message"],
            is_error=not result["success"],
        )

    return safe_tool_call("launch_app", _do)


def check_app_installed(project_id: int, *, app_name: str) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_check_app_installed(project_id, app_name)
        return ToolResult(
            tool_call_id="",
            content=result["message"],
            is_error=not result["success"],
        )

    return safe_tool_call("check_app_installed", _do)


# ============================================================================
# BROWSER TOOLS
# ============================================================================


def browser_navigate(project_id: int, *, url: str) -> ToolResult:
    def _do() -> ToolResult:
        controller_browser_navigate(project_id, url)
        return ToolResult(
            tool_call_id="",
            content=f"Navigated browser to {url}",
            is_error=False,
        )

    return safe_tool_call("browser_navigate", _do)


def browser_click(
    project_id: int,
    *,
    description: str,
    vision_config: LLMConfig,
) -> ToolResult:
    def _do() -> ToolResult:
        idx = find_element_index(project_id, description, vision_config)
        controller_browser_click(project_id, idx)
        return ToolResult(
            tool_call_id="",
            content=f"Clicked browser element [{idx}]: {description}",
            is_error=False,
        )

    return safe_tool_call("browser_click", _do)


def browser_type(
    project_id: int,
    *,
    description: str,
    text: str,
    vision_config: LLMConfig,
) -> ToolResult:
    def _do() -> ToolResult:
        idx = find_element_index(project_id, description, vision_config)
        controller_browser_type(project_id, idx, text)
        return ToolResult(
            tool_call_id="",
            content=f"Typed '{text}' into browser element [{idx}]: {description}",
            is_error=False,
        )

    return safe_tool_call("browser_type", _do)


def browser_hover(
    project_id: int,
    *,
    description: str,
    vision_config: LLMConfig,
) -> ToolResult:
    def _do() -> ToolResult:
        idx = find_element_index(project_id, description, vision_config)
        controller_browser_hover(project_id, idx)
        return ToolResult(
            tool_call_id="",
            content=f"Hovered browser element [{idx}]: {description}",
            is_error=False,
        )

    return safe_tool_call("browser_hover", _do)


def browser_get_page_content(project_id: int) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_browser_get_page_content(project_id)
        return ToolResult(
            tool_call_id="",
            content=result["content"],
            is_error=not result["success"],
        )

    return safe_tool_call("browser_get_page_content", _do)


def browser_get_url(project_id: int) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_browser_get_url(project_id)
        return ToolResult(
            tool_call_id="",
            content=result["content"],
            is_error=not result["success"],
        )

    return safe_tool_call("browser_get_url", _do)


def browser_download(project_id: int, *, url: str, save_path: str = "") -> ToolResult:
    def _do() -> ToolResult:
        result = controller_browser_download(project_id, url, save_path)
        return ToolResult(
            tool_call_id="",
            content=result["message"],
            is_error=not result["success"],
        )

    return safe_tool_call("browser_download", _do)


def browser_list_downloads(project_id: int) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_browser_list_downloads(project_id)
        return ToolResult(
            tool_call_id="",
            content=result["message"],
            is_error=not result["success"],
        )

    return safe_tool_call("browser_list_downloads", _do)


def browser_take_screenshot(
    project_id: int,
    *,
    question: str,
    vision_config: LLMConfig,
    on_screenshot: ScreenshotCallback | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_browser_take_screenshot(project_id)
        image_base64 = result["image_base64"]
        if on_screenshot is not None:
            on_screenshot(image_base64, "browser_take_screenshot")
        answer = answer_screenshot_question(vision_config, image_base64, question)
        return ToolResult(tool_call_id="", content=answer, is_error=False)

    return safe_tool_call("browser_take_screenshot", _do)
