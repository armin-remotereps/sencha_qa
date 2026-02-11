from __future__ import annotations

import logging

from agents.services.ssh_session import SSHSessionManager
from agents.services.vision_qa import answer_screenshot_question
from agents.types import DMRConfig, ToolResult

logger = logging.getLogger(__name__)

DISPLAY_ENV = "DISPLAY=:0"


def take_screenshot(
    ssh_session: SSHSessionManager,
    *,
    question: str,
    vision_config: DMRConfig,
) -> ToolResult:
    """Capture desktop screenshot and answer a question about it."""
    try:
        capture_cmd = f"{DISPLAY_ENV} scrot -o /tmp/screenshot.png"
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
        answer = answer_screenshot_question(vision_config, image_base64, question)
        return ToolResult(
            tool_call_id="",
            content=answer,
            is_error=False,
        )
    except Exception as e:
        logger.error("Screenshot failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Screenshot error: {e}",
            is_error=True,
        )


def screen_click(
    ssh_session: SSHSessionManager, *, x: int, y: int, button: int = 1
) -> ToolResult:
    """Click at (x, y) on the desktop using xdotool."""
    try:
        cmd = f"{DISPLAY_ENV} xdotool mousemove {x} {y} click {button}"
        result = ssh_session.execute(cmd)
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"Click failed: {result.stderr}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=f"Clicked at ({x}, {y}) with button {button}.",
            is_error=False,
        )
    except Exception as e:
        logger.error("Screen click failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Click error: {e}",
            is_error=True,
        )


def screen_type_text(ssh_session: SSHSessionManager, *, text: str) -> ToolResult:
    """Type text using xdotool."""
    try:
        escaped_text = text.replace("'", "'\\''")
        cmd = f"{DISPLAY_ENV} xdotool type -- '{escaped_text}'"
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
    except Exception as e:
        logger.error("Screen type text failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Type text error: {e}",
            is_error=True,
        )


def screen_key_press(ssh_session: SSHSessionManager, *, keys: str) -> ToolResult:
    """Press key combination using xdotool."""
    try:
        cmd = f"{DISPLAY_ENV} xdotool key {keys}"
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
    except Exception as e:
        logger.error("Screen key press failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Key press error: {e}",
            is_error=True,
        )


def screen_list_windows(ssh_session: SSHSessionManager) -> ToolResult:
    """List all windows using wmctrl."""
    try:
        cmd = f"{DISPLAY_ENV} wmctrl -l"
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
    except Exception as e:
        logger.error("List windows failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"List windows error: {e}",
            is_error=True,
        )


def screen_get_active_window(ssh_session: SSHSessionManager) -> ToolResult:
    """Get the active window info using xdotool."""
    try:
        cmd = f"{DISPLAY_ENV} xdotool getactivewindow getwindowname"
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
    except Exception as e:
        logger.error("Get active window failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Get active window error: {e}",
            is_error=True,
        )
