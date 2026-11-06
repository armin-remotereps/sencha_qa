"""Phone and tablet actions (MOBILE_DEVICE_V1).

Each action runs on the selected phone target (``target.get_mobile_device``)
and answers like ``executor.py``: an ``ActionResultPayload`` whose message
names the screenshot point. Points arrive in screenshot pixels and are mapped
to the device's input space with ``executor.to_input``. On a desktop target
every action raises TargetMismatchError; a device that is gone raises
DeviceDisconnectedError (from the backend), both passed through unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from controller_client.exceptions import (
    DeviceDisconnectedError,
    ExecutionError,
    TargetMismatchError,
)
from controller_client.executor import to_input
from controller_client.mobile_device import MobileDevice
from controller_client.protocol import (
    ActionResultPayload,
    CheckAppInstalledPayload,
    CloseAppPayload,
    InstallAppPayload,
    LaunchAppPayload,
    LongPressPayload,
    OpenUrlPayload,
    PressButtonPayload,
    RotateDevicePayload,
    SwipePayload,
)
from controller_client.target import get_mobile_device

_T = TypeVar("_T")

_NOT_A_PHONE = "This Test Runner drives a desktop, not a phone or tablet"


def _device() -> MobileDevice:
    device = get_mobile_device()
    if device is None:
        raise TargetMismatchError(_NOT_A_PHONE)
    return device


def _run(action: str, call: Callable[[], _T]) -> _T:
    """Run a device call; unexpected failures become ExecutionError."""
    try:
        return call()
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"{action} failed: {e}") from e


def _result(message: str, start: float, success: bool = True) -> ActionResultPayload:
    return ActionResultPayload(
        success=success,
        message=message,
        duration_ms=(time.monotonic() - start) * 1000,
    )


def _point(x: int, y: int, input_x: int, input_y: int) -> str:
    # Name the input point only when it differs, so logs show any scaling.
    if (x, y) == (input_x, input_y):
        return f"({x}, {y})"
    return f"({x}, {y}) [input ({input_x}, {input_y})]"


def _empty_app_name(start: float) -> ActionResultPayload:
    return _result("app_name must not be empty.", start, success=False)


def execute_swipe(payload: SwipePayload) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    if payload.x is not None and payload.y is not None:
        x, y = to_input(payload.x, payload.y)
        input_x: int | None = x
        input_y: int | None = y
        where = f"from {_point(payload.x, payload.y, x, y)}"
    else:
        input_x = input_y = None
        where = "from the centre of the screen"
    _run(
        "Swipe",
        lambda: device.swipe(
            payload.direction, input_x, input_y, payload.distance, payload.duration
        ),
    )
    return _result(
        f"Swiped {payload.direction} {where} over {payload.distance:.0%} of the "
        f"screen in {payload.duration:.2f}s",
        start,
    )


def execute_long_press(payload: LongPressPayload) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    x, y = to_input(payload.x, payload.y)
    _run("Long press", lambda: device.long_press(x, y, payload.duration))
    return _result(
        f"Long-pressed {_point(payload.x, payload.y, x, y)} "
        f"for {payload.duration:.1f}s",
        start,
    )


def execute_press_button(payload: PressButtonPayload) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    _run("Button press", lambda: device.press_button(payload.button))
    return _result(f"Pressed the {payload.button.value} button", start)


def execute_rotate_device(payload: RotateDevicePayload) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    _run("Rotate", lambda: device.rotate(payload.orientation))
    return _result(f"Rotated the device to {payload.orientation.value}", start)


def execute_hide_keyboard() -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    hidden = _run("Hide keyboard", device.hide_keyboard)
    message = "Hid the keyboard" if hidden else "No keyboard was shown"
    return _result(message, start)


def execute_open_url(payload: OpenUrlPayload) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    _run("Open URL", lambda: device.open_url(payload.url))
    return _result(f"Opened {payload.url} on the device", start)


def execute_install_app(payload: InstallAppPayload) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    app_id = _run("Install app", lambda: device.install_app(payload.path))
    installed = app_id or "the app"
    return _result(f"Installed {installed} from {payload.path}", start)


def execute_device_launch_app(payload: LaunchAppPayload) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    query = payload.app_name.strip()
    if not query:
        return _empty_app_name(start)
    app_id = _run("Launch app", lambda: device.launch_app(query))
    return _result(f"Launched {app_id} on the device", start)


def execute_device_close_app(payload: CloseAppPayload) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    query = payload.app_name.strip()
    if not query:
        return _empty_app_name(start)
    message = _run("Close app", lambda: device.close_app(query, payload.force))
    return _result(message, start)


def execute_device_check_app_installed(
    payload: CheckAppInstalledPayload,
) -> ActionResultPayload:
    device = _device()
    start = time.monotonic()
    query = payload.app_name.strip()
    if not query:
        return _empty_app_name(start)
    app_id = _run("App check", lambda: device.is_app_installed(query))
    # Same prefixes as the desktop check (app_checker), which the server reads.
    if app_id is None:
        return _result(f"NOT INSTALLED: '{query}' was not found on the device.", start)
    return _result(f"INSTALLED: '{query}' found on the device as {app_id}", start)


def execute_device_cleanup(host: ActionResultPayload) -> ActionResultPayload:
    """Add the device cleanup to the host cleanup's result (Q5a).

    Goes home (which dismisses the keyboard), then terminates the apps
    launched since the last cleanup while time remains. A failure is reported in the message and fails the
    cleanup, like a host step; a device that is gone still raises.
    """
    device = _device()
    start = time.monotonic()
    success = host.success
    try:
        lines = device.cleanup()
        device_part = "device: " + ("; ".join(lines) if lines else "nothing to do")
    except DeviceDisconnectedError:
        raise
    except Exception as e:
        device_part = f"device cleanup failed: {e}"
        success = False
    return ActionResultPayload(
        success=success,
        message=f"{host.message}; {device_part}",
        duration_ms=host.duration_ms + (time.monotonic() - start) * 1000,
    )
