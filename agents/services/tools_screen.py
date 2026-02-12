from __future__ import annotations

from collections.abc import Callable

from agents.services.ssh_session import SSHSessionManager
from agents.services.tool_utils import safe_tool_call
from agents.services.vision_qa import answer_screenshot_question
from agents.types import DMRConfig, ToolResult


def take_screenshot(
    ssh_session: SSHSessionManager,
    *,
    question: str,
    vision_config: DMRConfig,
    on_screenshot: Callable[[str, str], None] | None = None,
) -> ToolResult:
    def _do() -> ToolResult:
        capture_cmd = "scrot -o /tmp/screenshot.png"
        capture_result = ssh_session.execute(capture_cmd)
        if capture_result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"Screenshot capture failed: {capture_result.stderr}",
                is_error=True,
            )

        read_cmd = "base64 -w 0 /tmp/screenshot.png"
        read_result = ssh_session.execute(read_cmd)
        if read_result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"Screenshot read failed: {read_result.stderr}",
                is_error=True,
            )

        image_base64 = read_result.stdout.strip()
        if on_screenshot is not None:
            on_screenshot(image_base64, "take_screenshot")
        answer = answer_screenshot_question(vision_config, image_base64, question)
        return ToolResult(
            tool_call_id="",
            content=answer,
            is_error=False,
        )

    return safe_tool_call("Screenshot", _do)


def screen_type_text(ssh_session: SSHSessionManager, *, text: str) -> ToolResult:
    def _do() -> ToolResult:
        escaped_text = text.replace("'", "'\\''")
        cmd = f"xdotool type -- '{escaped_text}'"
        result = ssh_session.execute(cmd)
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"Type text failed: {result.stderr}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=f"Typed text: {text}",
            is_error=False,
        )

    return safe_tool_call("Type text", _do)


def screen_key_press(ssh_session: SSHSessionManager, *, keys: str) -> ToolResult:
    def _do() -> ToolResult:
        cmd = f"xdotool key {keys}"
        result = ssh_session.execute(cmd)
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"Key press failed: {result.stderr}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=f"Pressed keys: {keys}",
            is_error=False,
        )

    return safe_tool_call("Key press", _do)


def screen_list_windows(ssh_session: SSHSessionManager) -> ToolResult:
    def _do() -> ToolResult:
        cmd = "wmctrl -l"
        result = ssh_session.execute(cmd)
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"List windows failed: {result.stderr}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=result.stdout if result.stdout else "No windows found.",
            is_error=False,
        )

    return safe_tool_call("List windows", _do)


def screen_get_active_window(ssh_session: SSHSessionManager) -> ToolResult:
    def _do() -> ToolResult:
        cmd = "xdotool getactivewindow getwindowname"
        result = ssh_session.execute(cmd)
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"Get active window failed: {result.stderr}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=result.stdout.strip() if result.stdout else "No active window.",
            is_error=False,
        )

    return safe_tool_call("Get active window", _do)
