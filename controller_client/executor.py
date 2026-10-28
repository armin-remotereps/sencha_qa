import base64
import io
import os
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Final

from PIL import Image

from controller_client.desktop import get_desktop_backend
from controller_client.desktop_backend import DesktopBackend
from controller_client.exceptions import ExecutionError
from controller_client.input_guard import ensure_input_not_blocked
from controller_client.interactive_session import InteractiveSessionManager
from controller_client.pointer_mapping import ScreenSpaces, map_to_input
from controller_client.process_tracker import ProcessTracker
from controller_client.protocol import (
    ActionResultPayload,
    ClickPayload,
    CommandResultPayload,
    DoubleClickPayload,
    DragPayload,
    HoverPayload,
    InteractiveOutputPayload,
    KeyPressPayload,
    RunCommandPayload,
    ScreenshotResponsePayload,
    ScrollPayload,
    SendInputPayload,
    StartInteractiveCmdPayload,
    StreamName,
    TerminateInteractiveCmdPayload,
    TypeTextPayload,
    WaitForCommandPayload,
)
from controller_client.screen_capture import capture_screen
from controller_client.shell_encoding import SHELL_OUTPUT_ENCODING

_DESKTOP: Final[DesktopBackend] = get_desktop_backend()


def to_input(x: int, y: int) -> tuple[int, int]:
    """Map a point from the latest screenshot into the backend's input space."""
    spaces = ScreenSpaces(_DESKTOP.capture_size(), _DESKTOP.input_size())
    return map_to_input(x, y, spaces)


def _point(x: int, y: int, input_x: int, input_y: int) -> str:
    # Name the input point only when it differs, so logs show any scaling.
    if (x, y) == (input_x, input_y):
        return f"({x}, {y})"
    return f"({x}, {y}) [input ({input_x}, {input_y})]"


def execute_click(payload: ClickPayload) -> ActionResultPayload:
    ensure_input_not_blocked()
    start = time.monotonic()
    x, y = to_input(payload.x, payload.y)
    try:
        _DESKTOP.click(x, y, payload.button)
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Click failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=(
            f"Clicked {_point(payload.x, payload.y, x, y)} "
            f"with {payload.button} button"
        ),
        duration_ms=duration_ms,
    )


def execute_double_click(payload: DoubleClickPayload) -> ActionResultPayload:
    ensure_input_not_blocked()
    start = time.monotonic()
    x, y = to_input(payload.x, payload.y)
    try:
        _DESKTOP.double_click(x, y)
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Double-click failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Double-clicked {_point(payload.x, payload.y, x, y)}",
        duration_ms=duration_ms,
    )


def execute_hover(payload: HoverPayload) -> ActionResultPayload:
    ensure_input_not_blocked()
    start = time.monotonic()
    x, y = to_input(payload.x, payload.y)
    try:
        _DESKTOP.move_to(x, y)
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Hover failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Hovered to {_point(payload.x, payload.y, x, y)}",
        duration_ms=duration_ms,
    )


def execute_drag(payload: DragPayload) -> ActionResultPayload:
    ensure_input_not_blocked()
    start = time.monotonic()
    start_x, start_y = to_input(payload.start_x, payload.start_y)
    end_x, end_y = to_input(payload.end_x, payload.end_y)
    try:
        _DESKTOP.drag(
            start_x,
            start_y,
            end_x,
            end_y,
            payload.button,
            payload.duration,
        )
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Drag failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=(
            f"Dragged from {_point(payload.start_x, payload.start_y, start_x, start_y)} "
            f"to {_point(payload.end_x, payload.end_y, end_x, end_y)}"
        ),
        duration_ms=duration_ms,
    )


def execute_scroll(payload: ScrollPayload) -> ActionResultPayload:
    # Windows UIPI drops wheel events too.
    ensure_input_not_blocked()
    start = time.monotonic()
    if payload.x is not None and payload.y is not None:
        x, y = to_input(payload.x, payload.y)
        input_x: int | None = x
        input_y: int | None = y
        where = f"at {_point(payload.x, payload.y, x, y)}"
    else:
        input_x = input_y = None
        where = "at the pointer"
    try:
        _DESKTOP.scroll(payload.direction, payload.amount, input_x, input_y)
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Scroll failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    notches = "notch" if payload.amount == 1 else "notches"
    return ActionResultPayload(
        success=True,
        message=f"Scrolled {payload.direction} {payload.amount} {notches} {where}",
        duration_ms=duration_ms,
    )


def execute_type_text(payload: TypeTextPayload) -> ActionResultPayload:
    ensure_input_not_blocked()
    start = time.monotonic()
    try:
        _DESKTOP.type_text(payload.text, payload.interval)
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Type text failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Typed {len(payload.text)} characters",
        duration_ms=duration_ms,
    )


# Each backend owns its key aliases (cmd/meta/super, opt, control, ...) and
# rejects names it cannot press, so an unknown key never becomes a bare letter.
def _normalize_key(key: str, *, in_combination: bool) -> str:
    if not key:
        raise ExecutionError(
            "Empty key in combination; the '+' key itself cannot be pressed"
        )
    # A lone uppercase letter means shift+letter to the backend; inside a
    # combination "cmd+N" means Cmd+N, so lowercase it there.
    name = key.lower() if len(key) > 1 or in_combination else key
    resolved = _DESKTOP.resolve_key(name)
    if resolved is None:
        raise ExecutionError(f"Unknown key: {key!r}")
    return resolved


def execute_key_press(payload: KeyPressPayload) -> ActionResultPayload:
    ensure_input_not_blocked()
    start = time.monotonic()
    parts = [k.strip() for k in payload.keys.split("+")]
    keys = [_normalize_key(k, in_combination=len(parts) > 1) for k in parts]
    try:
        _DESKTOP.press_keys(keys)
    except ExecutionError:
        raise
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


def _execute_background_command(
    command: str, process_tracker: ProcessTracker
) -> CommandResultPayload:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,  # noqa: S602
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        process_tracker.register(proc.pid)
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


def execute_command(
    payload: RunCommandPayload, process_tracker: ProcessTracker
) -> CommandResultPayload:
    if _is_background_command(payload.command):
        return _execute_background_command(payload.command, process_tracker)

    start = time.monotonic()
    try:
        completed = subprocess.run(
            payload.command,
            shell=True,  # noqa: S602
            capture_output=True,
            encoding=SHELL_OUTPUT_ENCODING,
            errors="replace",
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
    process_tracker: ProcessTracker,
) -> CommandResultPayload:
    if _is_background_command(payload.command):
        return _execute_background_command(payload.command, process_tracker)

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    start = time.monotonic()
    try:
        process = subprocess.Popen(
            payload.command,
            shell=True,  # noqa: S602
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding=SHELL_OUTPUT_ENCODING,
            errors="replace",
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
        screenshot: Image.Image = capture_screen()
        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG")
        image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        width, height = screenshot.size
    except ExecutionError:
        # ScreenCaptureError or DesktopSessionError: already carries the
        # OS-level reason and how to fix it.
        raise
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
    process_tracker: ProcessTracker,
) -> InteractiveOutputPayload:
    try:
        session = session_manager.start_session(payload.command, timeout)
        output = session.start(process_tracker)
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
