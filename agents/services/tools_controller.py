from __future__ import annotations

from agents.services.browser_element_finder import find_element_index
from agents.services.controller_element_finder import find_element_coordinates
from agents.services.tool_utils import safe_tool_call
from agents.services.vision_qa import answer_screenshot_question
from agents.types import DMRConfig, ScreenshotCallback, ToolResult
from projects.services import (
    controller_browser_click,
    controller_browser_get_page_content,
    controller_browser_get_url,
    controller_browser_hover,
    controller_browser_navigate,
    controller_browser_take_screenshot,
    controller_browser_type,
    controller_click,
    controller_drag,
    controller_hover,
    controller_key_press,
    controller_run_command,
    controller_screenshot,
    controller_type_text,
)


def execute_command(project_id: int, *, command: str) -> ToolResult:
    def _do() -> ToolResult:
        result = controller_run_command(project_id, command)
        parts: list[str] = []
        if result["stdout"]:
            parts.append(result["stdout"])
        if result["stderr"]:
            parts.append(f"STDERR: {result['stderr']}")
        parts.append(f"Exit code: {result['return_code']}")
        return ToolResult(
            tool_call_id="",
            content="\n".join(parts),
            is_error=result["return_code"] != 0,
        )

    return safe_tool_call("execute_command", _do)


def take_screenshot(
    project_id: int,
    *,
    question: str,
    vision_config: DMRConfig,
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


def click(
    project_id: int,
    *,
    description: str,
    vision_config: DMRConfig,
    on_screenshot: ScreenshotCallback | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        x, y = find_element_coordinates(
            project_id, description, vision_config, on_screenshot=on_screenshot
        )
        controller_click(project_id, x, y)
        return ToolResult(
            tool_call_id="",
            content=f"Clicked element at ({x}, {y}): {description}",
            is_error=False,
        )

    return safe_tool_call("click", _do)


def type_text(project_id: int, *, text: str) -> ToolResult:
    def _do() -> ToolResult:
        controller_type_text(project_id, text)
        return ToolResult(
            tool_call_id="", content=f"Typed text: {text}", is_error=False
        )

    return safe_tool_call("type_text", _do)


def key_press(project_id: int, *, keys: str) -> ToolResult:
    def _do() -> ToolResult:
        controller_key_press(project_id, keys)
        return ToolResult(
            tool_call_id="", content=f"Pressed keys: {keys}", is_error=False
        )

    return safe_tool_call("key_press", _do)


def hover(
    project_id: int,
    *,
    description: str,
    vision_config: DMRConfig,
    on_screenshot: ScreenshotCallback | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        x, y = find_element_coordinates(
            project_id, description, vision_config, on_screenshot=on_screenshot
        )
        controller_hover(project_id, x, y)
        return ToolResult(
            tool_call_id="",
            content=f"Hovered over element at ({x}, {y}): {description}",
            is_error=False,
        )

    return safe_tool_call("hover", _do)


def drag(
    project_id: int,
    *,
    start_description: str,
    end_description: str,
    vision_config: DMRConfig,
    on_screenshot: ScreenshotCallback | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        sx, sy = find_element_coordinates(
            project_id,
            start_description,
            vision_config,
            on_screenshot=on_screenshot,
        )
        ex, ey = find_element_coordinates(
            project_id,
            end_description,
            vision_config,
            on_screenshot=on_screenshot,
        )
        controller_drag(project_id, sx, sy, ex, ey)
        return ToolResult(
            tool_call_id="",
            content=f"Dragged from ({sx}, {sy}) to ({ex}, {ey})",
            is_error=False,
        )

    return safe_tool_call("drag", _do)


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
    dmr_config: DMRConfig,
) -> ToolResult:
    def _do() -> ToolResult:
        idx = find_element_index(project_id, description, dmr_config)
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
    dmr_config: DMRConfig,
) -> ToolResult:
    def _do() -> ToolResult:
        idx = find_element_index(project_id, description, dmr_config)
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
    dmr_config: DMRConfig,
) -> ToolResult:
    def _do() -> ToolResult:
        idx = find_element_index(project_id, description, dmr_config)
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


def browser_take_screenshot(
    project_id: int,
    *,
    question: str,
    vision_config: DMRConfig,
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
