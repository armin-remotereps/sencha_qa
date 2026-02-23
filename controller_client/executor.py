import base64
import io
import os
import subprocess
import threading
import time
from collections.abc import Callable

import pyautogui
from PIL import Image

from controller_client.exceptions import ExecutionError
from controller_client.interactive_session import InteractiveSessionManager
from controller_client.protocol import (
    ActionResultPayload,
    ClickPayload,
    CommandResultPayload,
    DragPayload,
    HoverPayload,
    InteractiveOutputPayload,
    KeyPressPayload,
    RunCommandPayload,
    ScreenshotResponsePayload,
    SendInputPayload,
    StartInteractiveCmdPayload,
    StreamName,
    TerminateInteractiveCmdPayload,
    TypeTextPayload,
    WaitForCommandPayload,
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


def _is_background_command(command: str) -> bool:
    stripped = command.rstrip()
    return stripped.endswith("&") and not stripped.endswith("&&")


def _execute_background_command(command: str) -> CommandResultPayload:
    start = time.monotonic()
    try:
        subprocess.Popen(
            command,
            shell=True,  # noqa: S602
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        raise ExecutionError(f"Background command failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return CommandResultPayload(
        success=True,
        stdout="",
        stderr="",
        return_code=0,
        duration_ms=duration_ms,
    )


_NON_INTERACTIVE_TIMEOUT_SECONDS = 120


def execute_command(payload: RunCommandPayload) -> CommandResultPayload:
    if _is_background_command(payload.command):
        return _execute_background_command(payload.command)

    start = time.monotonic()
    try:
        completed = subprocess.run(
            payload.command,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=_NON_INTERACTIVE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - start) * 1000
        return CommandResultPayload(
            success=False,
            stdout="",
            stderr=(
                f"Command timed out after {_NON_INTERACTIVE_TIMEOUT_SECONDS}s. "
                "This usually means the command is waiting for input (password, "
                "confirmation, etc.). Use start_interactive_command instead of "
                "execute_command for commands that require user input."
            ),
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


def _read_stream(
    stream: io.TextIOWrapper,
    stream_name: StreamName,
    lines: list[str],
    on_output: Callable[[str, StreamName], None],
) -> None:
    for line in stream:
        lines.append(line)
        on_output(line, stream_name)


def execute_command_streaming(
    payload: RunCommandPayload,
    on_output: Callable[[str, StreamName], None],
) -> CommandResultPayload:
    if _is_background_command(payload.command):
        return _execute_background_command(payload.command)

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    start = time.monotonic()
    try:
        process = subprocess.Popen(
            payload.command,
            shell=True,  # noqa: S602
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except Exception as e:
        raise ExecutionError(f"Command execution failed: {e}") from e

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    if process.stdout is None or process.stderr is None:
        raise ExecutionError("Process streams were not captured")

    stdout_thread = threading.Thread(
        target=_read_stream,
        args=(process.stdout, StreamName.STDOUT, stdout_lines, on_output),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stream,
        args=(process.stderr, StreamName.STDERR, stderr_lines, on_output),
        daemon=True,
    )

    stdout_thread.start()
    stderr_thread.start()

    process.wait()
    stdout_thread.join()
    stderr_thread.join()

    process.stdout.close()
    process.stderr.close()

    duration_ms = (time.monotonic() - start) * 1000
    return CommandResultPayload(
        success=process.returncode == 0,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        return_code=process.returncode,
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


def execute_start_interactive_cmd(
    session_manager: InteractiveSessionManager,
    payload: StartInteractiveCmdPayload,
    timeout: float,
) -> InteractiveOutputPayload:
    try:
        session = session_manager.start_session(payload.command, timeout)
        output = session.start()
    except Exception as e:
        raise ExecutionError(f"Start interactive command failed: {e}") from e
    return InteractiveOutputPayload(
        session_id=session.session_id,
        output=output,
        is_alive=session.is_alive(),
        exit_code=session.exit_code(),
        duration_ms=session.elapsed_ms(),
    )


def execute_send_input(
    session_manager: InteractiveSessionManager,
    payload: SendInputPayload,
) -> InteractiveOutputPayload:
    try:
        session = session_manager.get_session(payload.session_id)
        output = session.send_input(payload.input_text)
    except Exception as e:
        raise ExecutionError(f"Send input failed: {e}") from e
    return InteractiveOutputPayload(
        session_id=session.session_id,
        output=output,
        is_alive=session.is_alive(),
        exit_code=session.exit_code(),
        duration_ms=session.elapsed_ms(),
    )


def execute_wait_for_command(
    session_manager: InteractiveSessionManager,
    payload: WaitForCommandPayload,
) -> InteractiveOutputPayload:
    try:
        session = session_manager.get_session(payload.session_id)
        chunks: list[str] = []
        while session.is_alive():
            chunk = session.read_output()
            if chunk:
                chunks.append(chunk)
        final = session.read_output()
        if final:
            chunks.append(final)
    except Exception as e:
        raise ExecutionError(f"Wait for command failed: {e}") from e
    return InteractiveOutputPayload(
        session_id=session.session_id,
        output="".join(chunks),
        is_alive=session.is_alive(),
        exit_code=session.exit_code(),
        duration_ms=session.elapsed_ms(),
    )


def execute_terminate_interactive_cmd(
    session_manager: InteractiveSessionManager,
    payload: TerminateInteractiveCmdPayload,
) -> InteractiveOutputPayload:
    try:
        session = session_manager.terminate_session(payload.session_id)
    except Exception as e:
        raise ExecutionError(f"Terminate interactive command failed: {e}") from e
    return InteractiveOutputPayload(
        session_id=session.session_id,
        output="",
        is_alive=False,
        exit_code=session.exit_code(),
        duration_ms=session.elapsed_ms(),
    )
