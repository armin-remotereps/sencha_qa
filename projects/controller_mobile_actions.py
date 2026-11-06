"""Phone and tablet actions sent to a MOBILE_DEVICE_V1 Test Runner.

docs/plan/feat-mobile-device-runner.md (Services). Each mirrors the desktop
``controller_*`` functions of ``projects.services``: coordinates are
screenshot pixels, the runner answers ``action_result`` or ``error``, and a
device that is gone raises ``DeviceDisconnectedError``. Every action but
``install_app`` is runner-local, so a runner that stops answering it blocks
the case as unresponsive rather than letting the agent judge the app.
"""

from __future__ import annotations

from typing import Final

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    ARTIFACT_PATH_MAX_CHARS,
    DeviceOrientation,
    MobileButton,
    ScrollDirection,
    is_network_path,
    validate_open_url,
)
from projects.exceptions import ControllerActionError
from projects.services import (
    ActionResult,
    build_action_result,
    dispatch_controller_action,
)

# Installing an app (copy, verify, install) depends on its size and the
# device, not only on the runner: a plain timeout the agent can judge.
INSTALL_APP_TIMEOUT_SECONDS: Final[float] = 300


def controller_swipe(
    project_id: int,
    direction: ScrollDirection,
    x: int | None,
    y: int | None,
    distance: float,
    duration: float,
    timeout: float = 30.0,
) -> ActionResult:
    """Swipe the finger ``direction`` from screenshot pixel (x, y), or from the
    screen's centre when both are None.

    ``x``/``y`` are sent only when given: the message builder skips keys the
    event lacks.
    """
    if x is None or y is None:
        reply = dispatch_controller_action(
            project_id,
            "controller.swipe",
            timeout,
            runner_local=True,
            direction=direction.value,
            distance=distance,
            duration=duration,
        )
    else:
        reply = dispatch_controller_action(
            project_id,
            "controller.swipe",
            timeout,
            runner_local=True,
            direction=direction.value,
            distance=distance,
            duration=duration,
            x=x,
            y=y,
        )
    return build_action_result(reply)


def controller_long_press(
    project_id: int,
    x: int,
    y: int,
    duration: float,
    timeout: float = 30.0,
) -> ActionResult:
    reply = dispatch_controller_action(
        project_id,
        "controller.long_press",
        timeout,
        runner_local=True,
        x=x,
        y=y,
        duration=duration,
    )
    return build_action_result(reply)


def controller_press_button(
    project_id: int,
    button: MobileButton,
    timeout: float = 30.0,
) -> ActionResult:
    reply = dispatch_controller_action(
        project_id,
        "controller.press_button",
        timeout,
        runner_local=True,
        button=button.value,
    )
    return build_action_result(reply)


def controller_rotate_device(
    project_id: int,
    orientation: DeviceOrientation,
    timeout: float = 30.0,
) -> ActionResult:
    reply = dispatch_controller_action(
        project_id,
        "controller.rotate_device",
        timeout,
        runner_local=True,
        orientation=orientation.value,
    )
    return build_action_result(reply)


def controller_hide_keyboard(project_id: int, timeout: float = 30.0) -> ActionResult:
    reply = dispatch_controller_action(
        project_id,
        "controller.hide_keyboard",
        timeout,
        runner_local=True,
    )
    return build_action_result(reply)


def controller_open_url(
    project_id: int, url: str, timeout: float = 30.0
) -> ActionResult:
    """Open ``url`` (a web link or an app deep link) on the device.

    Raises ``ControllerActionError`` before anything is sent when the device
    must not open it (``javascript:``, ``file:``, ... or a malformed URL), as
    the runner would refuse it too.
    """
    try:
        checked = validate_open_url(url)
    except ProtocolError as exc:
        raise ControllerActionError(str(exc)) from exc
    reply = dispatch_controller_action(
        project_id,
        "controller.open_url",
        timeout,
        runner_local=True,
        url=checked,
    )
    return build_action_result(reply)


def controller_install_app(
    project_id: int,
    path: str,
    timeout: float = INSTALL_APP_TIMEOUT_SECONDS,
) -> ActionResult:
    """Install the app at ``path`` on the runner host (``.apk``, a simulator
    ``.app`` or a signed ``.ipa``) onto the device.

    Raises ``ControllerActionError`` before anything is sent for an empty,
    oversized or network path, which the runner would refuse too.
    """
    if (
        not path.strip()
        or len(path) > ARTIFACT_PATH_MAX_CHARS
        or "\x00" in path
        or is_network_path(path)
    ):
        raise ControllerActionError(
            "Invalid 'path': expected a local path on the Test Runner computer"
        )
    reply = dispatch_controller_action(
        project_id,
        "controller.install_app",
        timeout,
        path=path,
    )
    return build_action_result(reply)
