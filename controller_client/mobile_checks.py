"""Diagnose steps of a phone target, shared by ``diagnose --mobile`` and
``MobileBackend.diagnostic_steps``.

Prerequisites first (Node.js, Appium, the driver, adb / Xcode), then the
device itself (session, screenshot, points/pixels scale) and, only when the
person agrees, a test tap at the centre of the screen. ``run_diagnostics``
stops at the first failure, so the hard requirements come first.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Final

from controller_client.diagnostics import DiagnosticStep
from controller_client.exceptions import ExecutionError
from controller_client.mobile_appium_server import (
    NODE_REQUIREMENT,
    REQUIRED_DRIVER,
    appium_status,
    appium_version,
    driver_install_hint,
    installed_drivers,
    node_version,
    node_version_supported,
)
from controller_client.mobile_device import MobileTargetBackend
from controller_client.mobile_discovery import describe_missing_tools
from controller_client.mobile_settings import MobileSettings
from controller_client.pointer_mapping import ScreenSpaces, map_to_input
from controller_client.protocol import DeviceState, RunnerTarget

_TAP_PROMPT: Final[str] = (
    "Tap the centre of the phone's screen once now? Make sure nothing "
    "important is there. [y/N] "
)


def prerequisite_steps(settings: MobileSettings) -> tuple[DiagnosticStep, ...]:
    """Node.js, Appium, its driver and the platform tools on this host.

    With CONTROLLER_APPIUM_URL set, Appium runs elsewhere: only its /status
    is checked.
    """
    tools = DiagnosticStep("platform tools", lambda: _check_platform_tools(settings))
    if settings.appium_url:
        return (
            tools,
            DiagnosticStep(
                "Appium server", lambda: _check_remote_appium(settings.appium_url)
            ),
        )
    return (
        DiagnosticStep("Node.js", _check_node),
        DiagnosticStep("Appium", lambda: appium_version(settings.appium_command)),
        DiagnosticStep("Appium driver", lambda: _check_driver(settings)),
        tools,
    )


def device_steps(
    get: Callable[[], MobileTargetBackend], interactive: bool
) -> tuple[DiagnosticStep, ...]:
    """Session, screenshot, scale and (interactive, asked first) a test tap.

    ``get`` gives the backend when a step runs (``diagnose --mobile`` builds
    it only after discovery found the device).
    """
    steps = [
        DiagnosticStep("automation session", lambda: _check_session(get())),
        DiagnosticStep("screenshot", lambda: _check_screenshot(get())),
        DiagnosticStep("points and pixels", lambda: _check_scale(get())),
    ]
    if interactive:
        steps.append(DiagnosticStep("test tap", lambda: _check_tap(get())))
    return tuple(steps)


def _check_node() -> str:
    version = node_version()
    if version is None:
        raise RuntimeError(
            f"node was not found on PATH: install {NODE_REQUIREMENT} from "
            "https://nodejs.org"
        )
    text = ".".join(str(part) for part in version)
    if not node_version_supported(version):
        raise RuntimeError(f"Node.js {text} is too old: install {NODE_REQUIREMENT}")
    return text


def _check_driver(settings: MobileSettings) -> str:
    driver = REQUIRED_DRIVER[settings.target]
    drivers = installed_drivers(settings.appium_command)
    if driver not in drivers:
        raise RuntimeError(
            f"the Appium driver '{driver}' is not installed. "
            f"{driver_install_hint(driver)}"
        )
    return f"{driver} {drivers[driver]}".strip()


def _check_platform_tools(settings: MobileSettings) -> str:
    missing = describe_missing_tools(settings.target)
    if missing is not None:
        raise RuntimeError(missing)
    return "adb found" if settings.target is RunnerTarget.ANDROID else "Xcode found"


def _check_remote_appium(url: str) -> str:
    status = appium_status(url)
    if status is None:
        raise RuntimeError(f"no Appium server answers at {url}/status")
    build = status.get("build")
    version = build.get("version") if isinstance(build, Mapping) else None
    return f"{url} (Appium {version or 'unknown version'})"


def _check_session(backend: MobileTargetBackend) -> str:
    started = time.monotonic()
    backend.start()
    status = backend.status()
    if status.state is not DeviceState.READY:
        raise RuntimeError(f"{status.state.value}: {status.detail}")
    elapsed = time.monotonic() - started
    info = backend.device_info()
    return f"{info.platform} {info.os_version}, {info.name} in {elapsed:.1f} s"


def _check_screenshot(backend: MobileTargetBackend) -> str:
    started = time.monotonic()
    image = backend.screenshot()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return f"{image.width}x{image.height} in {elapsed_ms} ms"


def _check_scale(backend: MobileTargetBackend) -> str:
    capture = backend.capture_size()
    input_size = backend.input_size()
    if input_size[0] <= 0 or input_size[1] <= 0:
        raise RuntimeError("the input size is unknown")
    scale = capture[0] / input_size[0]
    return (
        f"screenshot {capture[0]}x{capture[1]} px, taps {input_size[0]}x"
        f"{input_size[1]} ({scale:.2g}x)"
    )


def _check_tap(backend: MobileTargetBackend) -> str:
    try:
        answer = input(_TAP_PROMPT)
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        return "skipped"
    width, height = backend.capture_size()
    spaces = ScreenSpaces(capture=(width, height), input=backend.input_size())
    x, y = map_to_input(width // 2, height // 2, spaces)
    try:
        backend.click(x, y, "left")
    except ExecutionError as exc:
        raise RuntimeError(str(exc)) from exc
    return f"tapped ({width // 2}, {height // 2}) px = ({x}, {y}) input"
