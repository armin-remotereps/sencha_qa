import base64
import io
import subprocess
import time

import pyautogui
from PIL import Image

from controller_client.exceptions import ExecutionError
from controller_client.protocol import (
    ActionResultPayload,
    ClickPayload,
    CommandResultPayload,
    DragPayload,
    HoverPayload,
    KeyPressPayload,
    RunCommandPayload,
    ScreenshotResponsePayload,
    TypeTextPayload,
)

pyautogui.FAILSAFE = False


def execute_click(payload: ClickPayload) -> ActionResultPayload:
    start = time.monotonic()
    try:
        pyautogui.click(x=payload.x, y=payload.y, button=payload.button)
    except Exception as e:
        raise ExecutionError(f"Click failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Clicked ({payload.x}, {payload.y}) with {payload.button} button",
        duration_ms=duration_ms,
    )


def execute_hover(payload: HoverPayload) -> ActionResultPayload:
    start = time.monotonic()
    try:
        pyautogui.moveTo(x=payload.x, y=payload.y)
    except Exception as e:
        raise ExecutionError(f"Hover failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Hovered to ({payload.x}, {payload.y})",
        duration_ms=duration_ms,
    )


def execute_drag(payload: DragPayload) -> ActionResultPayload:
    start = time.monotonic()
    try:
        pyautogui.moveTo(x=payload.start_x, y=payload.start_y)
        pyautogui.drag(
            xOffset=payload.end_x - payload.start_x,
            yOffset=payload.end_y - payload.start_y,
            duration=payload.duration,
            button=payload.button,
        )
    except Exception as e:
        raise ExecutionError(f"Drag failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=(
            f"Dragged from ({payload.start_x}, {payload.start_y}) "
            f"to ({payload.end_x}, {payload.end_y})"
        ),
        duration_ms=duration_ms,
    )


def execute_type_text(payload: TypeTextPayload) -> ActionResultPayload:
    start = time.monotonic()
    try:
        pyautogui.typewrite(payload.text, interval=payload.interval)
    except Exception as e:
        raise ExecutionError(f"Type text failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Typed {len(payload.text)} characters",
        duration_ms=duration_ms,
    )


def execute_key_press(payload: KeyPressPayload) -> ActionResultPayload:
    start = time.monotonic()
    try:
        keys = [k.strip() for k in payload.keys.split("+")]
        if len(keys) > 1:
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(keys[0])
    except Exception as e:
        raise ExecutionError(f"Key press failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Pressed key(s): {payload.keys}",
        duration_ms=duration_ms,
    )


def execute_command(payload: RunCommandPayload) -> CommandResultPayload:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            payload.command,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=payload.timeout,
        )
    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - start) * 1000
        return CommandResultPayload(
            success=False,
            stdout="",
            stderr=f"Command timed out after {payload.timeout}s",
            return_code=-1,
            duration_ms=duration_ms,
        )
    except Exception as e:
        raise ExecutionError(f"Command execution failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return CommandResultPayload(
        success=completed.returncode == 0,
        stdout=completed.stdout,
        stderr=completed.stderr,
        return_code=completed.returncode,
        duration_ms=duration_ms,
    )


def execute_screenshot() -> ScreenshotResponsePayload:
    start = time.monotonic()
    try:
        screenshot: Image.Image = pyautogui.screenshot()
        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG")
        image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        width, height = screenshot.size
    except Exception as e:
        raise ExecutionError(f"Screenshot failed: {e}") from e
    return ScreenshotResponsePayload(
        success=True,
        image_base64=image_base64,
        width=width,
        height=height,
        format="png",
    )
