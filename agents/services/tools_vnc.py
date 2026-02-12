from __future__ import annotations

import base64
from collections.abc import Callable

from agents.services.tool_utils import safe_tool_call
from agents.services.vision_qa import answer_screenshot_question
from agents.services.vnc_element_finder import find_element_coordinates
from agents.services.vnc_session import VncSessionManager
from agents.types import DMRConfig, ToolResult


def vnc_take_screenshot(
    vnc_session: VncSessionManager,
    *,
    question: str,
    vision_config: DMRConfig,
    on_screenshot: Callable[[str, str], None] | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        screenshot_bytes = vnc_session.capture_screen()
        image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        if on_screenshot is not None:
            on_screenshot(image_base64, "vnc_take_screenshot")
        answer = answer_screenshot_question(vision_config, image_base64, question)
        return ToolResult(
            tool_call_id="",
            content=answer,
            is_error=False,
        )

    return safe_tool_call("VNC screenshot", _do)


def vnc_click(
    vnc_session: VncSessionManager,
    *,
    description: str,
    vision_config: DMRConfig,
    on_screenshot: Callable[[str, str], None] | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        x, y = find_element_coordinates(
            vnc_session, description, vision_config, on_screenshot=on_screenshot
        )
        vnc_session.mouse_click(x, y)
        return ToolResult(
            tool_call_id="",
            content=f"Clicked element at ({x}, {y}): {description}",
            is_error=False,
        )

    return safe_tool_call("VNC click", _do)


def vnc_type(
    vnc_session: VncSessionManager,
    *,
    description: str,
    text: str,
    vision_config: DMRConfig,
    on_screenshot: Callable[[str, str], None] | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        x, y = find_element_coordinates(
            vnc_session, description, vision_config, on_screenshot=on_screenshot
        )
        vnc_session.mouse_click(x, y)
        vnc_session.type_text(text)
        return ToolResult(
            tool_call_id="",
            content=f"Typed '{text}' into element at ({x}, {y}): {description}",
            is_error=False,
        )

    return safe_tool_call("VNC type", _do)


def vnc_hover(
    vnc_session: VncSessionManager,
    *,
    description: str,
    vision_config: DMRConfig,
    on_screenshot: Callable[[str, str], None] | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        x, y = find_element_coordinates(
            vnc_session, description, vision_config, on_screenshot=on_screenshot
        )
        vnc_session.mouse_move(x, y)
        return ToolResult(
            tool_call_id="",
            content=f"Hovered over element at ({x}, {y}): {description}",
            is_error=False,
        )

    return safe_tool_call("VNC hover", _do)


def vnc_key_press(
    vnc_session: VncSessionManager,
    *,
    keys: str,
) -> ToolResult:
    def _do() -> ToolResult:
        vnc_session.key_press(keys)
        return ToolResult(
            tool_call_id="",
            content=f"Pressed keys via VNC: {keys}",
            is_error=False,
        )

    return safe_tool_call("VNC key_press", _do)
