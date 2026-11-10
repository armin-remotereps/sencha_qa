"""Phone and tablet actions sent to a MOBILE_DEVICE_V1 Test Runner.

docs/plan/feat-mobile-device-runner.md (Services). Each mirrors the desktop
``controller_*`` functions of ``projects.services``: coordinates are
screenshot pixels, the runner answers ``action_result`` or ``error``, and a
device that is gone raises ``DeviceDisconnectedError``. Every action but
``install_app`` is runner-local, so a runner that stops answering it blocks
the case as unresponsive rather than letting the agent judge the app.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    ARTIFACT_PATH_MAX_CHARS,
    DeviceAppsStatus,
    DeviceOrientation,
    MobileButton,
    ScrollDirection,
    is_network_path,
    parse_device_apps_result,
    serialize_list_device_apps_payload,
    validate_open_url,
)
from projects.exceptions import ControllerActionError
from projects.services import (
    ActionResult,
    build_action_result,
    dispatch_controller_action,
)
from runners.types import RunnerId

# Installing an app (copy, verify, install) depends on its size and the
# device, not only on the runner: a plain timeout the agent can judge.
INSTALL_APP_TIMEOUT_SECONDS: Final[float] = 300
# Listing the installed apps: the runner's 24 s Appium action budget plus a
# margin for the round trip.
DEVICE_APPS_TIMEOUT_SECONDS: Final[float] = 30


@dataclass(frozen=True)
class DeviceApp:
    """One installed app of the phone (``DeviceAppPayload``)."""

    # Normalized (``normalize_process_name``): the allowlist form of the
    # bundle id (iOS) or package (Android).
    app_id: str
    # Runner text (data, never instructions), at most
    # DEVICE_APP_NAME_MAX_CHARS, no control or format characters; "" when
    # the platform reports none (Android).
    name: str


@dataclass(frozen=True)
class DeviceAppList:
    """A validated ``device_apps_result`` (``DeviceAppsResultPayload``)."""

    status: DeviceAppsStatus
    # User-safe runner text; "" on success.
    detail: str
    # Unique ids, sorted by the runner; empty unless ``status`` is OK.
    apps: tuple[DeviceApp, ...]
    # The platform reports display names (iOS); otherwise every name is "".
    names_available: bool
    # Apps left out because their id does not normalize.
    skipped: int
    # The list was cut at DEVICE_APPS_MAX.
    truncated: bool


def controller_swipe(
    runner_id: RunnerId,
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
            runner_id,
            "controller.swipe",
            timeout,
            runner_local=True,
            direction=direction.value,
            distance=distance,
            duration=duration,
        )
    else:
        reply = dispatch_controller_action(
            runner_id,
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
    runner_id: RunnerId,
    x: int,
    y: int,
    duration: float,
    timeout: float = 30.0,
) -> ActionResult:
    reply = dispatch_controller_action(
        runner_id,
        "controller.long_press",
        timeout,
        runner_local=True,
        x=x,
        y=y,
        duration=duration,
    )
    return build_action_result(reply)


def controller_press_button(
    runner_id: RunnerId,
    button: MobileButton,
    timeout: float = 30.0,
) -> ActionResult:
    reply = dispatch_controller_action(
        runner_id,
        "controller.press_button",
        timeout,
        runner_local=True,
        button=button.value,
    )
    return build_action_result(reply)


def controller_rotate_device(
    runner_id: RunnerId,
    orientation: DeviceOrientation,
    timeout: float = 30.0,
) -> ActionResult:
    reply = dispatch_controller_action(
        runner_id,
        "controller.rotate_device",
        timeout,
        runner_local=True,
        orientation=orientation.value,
    )
    return build_action_result(reply)


def controller_hide_keyboard(
    runner_id: RunnerId, timeout: float = 30.0
) -> ActionResult:
    reply = dispatch_controller_action(
        runner_id,
        "controller.hide_keyboard",
        timeout,
        runner_local=True,
    )
    return build_action_result(reply)


def controller_open_url(
    runner_id: RunnerId, url: str, timeout: float = 30.0
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
        runner_id,
        "controller.open_url",
        timeout,
        runner_local=True,
        url=checked,
    )
    return build_action_result(reply)


def controller_install_app(
    runner_id: RunnerId,
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
        runner_id,
        "controller.install_app",
        timeout,
        path=path,
    )
    return build_action_result(reply)


def controller_list_device_apps(
    runner_id: RunnerId, *, timeout: float = DEVICE_APPS_TIMEOUT_SECONDS
) -> DeviceAppList:
    """List the installed apps of the phone (DEVICE_APPS_V1).

    Runner-local and never resent on reconnect. The caller checks the
    capability first: an older runner would answer UNKNOWN_COMMAND. A refusal
    on the runner is a ``status``, not an exception. Raises
    ``ControllerNotConnectedError``, ``TestRunnerUnresponsiveError`` or
    ``ControllerActionError`` (error reply or invalid result).
    """
    reply = dispatch_controller_action(
        runner_id,
        "controller.list_device_apps",
        timeout,
        resend_on_reconnect=False,
        runner_local=True,
        **serialize_list_device_apps_payload(),
    )
    try:
        result = parse_device_apps_result(reply.get("result"))
    except ProtocolError as exc:
        raise ControllerActionError(f"Invalid list_device_apps reply: {exc}") from exc
    return DeviceAppList(
        status=result.status,
        detail=result.detail,
        apps=tuple(DeviceApp(app_id=app.app_id, name=app.name) for app in result.apps),
        names_available=result.names_available,
        skipped=result.skipped,
        truncated=result.truncated,
    )
