"""The phone or tablet target: ``DesktopBackend`` + ``MobileDevice`` on Appium.

Shared paths (screenshot, tap via ``click``, double tap, drag, scroll as a
swipe, typing, a few keys, the sizes behind ``pointer_mapping``) and the
phone-only gestures and app lifecycle all run through one ``MobileSession``.
Platform specifics live in ``mobile_android`` / ``mobile_ios``. Pointer
coordinates arrive in input space: Android pixels, iOS points.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Final

from appium.webdriver.webdriver import WebDriver
from PIL import Image, UnidentifiedImageError
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchDriverException,
    WebDriverException,
)
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from controller_client import mobile_gestures as gestures
from controller_client.diagnostics import DiagnosticStep
from controller_client.exceptions import (
    DeviceDisconnectedError,
    ExecutionError,
    ScreenCaptureError,
)
from controller_client.mobile_app_policy import MobileAppPolicy
from controller_client.mobile_appium_server import ManagedAppiumServer
from controller_client.mobile_checks import device_steps, prerequisite_steps
from controller_client.mobile_discovery import DiscoveredDevice, find_device
from controller_client.mobile_platform import MobilePlatform
from controller_client.mobile_session import (
    CLEANUP_BUDGET_SECONDS,
    COMMAND_HTTP_TIMEOUT_SECONDS,
    MobileSession,
    bounded,
)
from controller_client.mobile_settings import MobileSettings
from controller_client.protocol import (
    DEFAULT_SWIPE_DURATION,
    DEVICE_STATUS_DETAIL_MAX_CHARS,
    SWIPE_MAX_DISTANCE,
    DeviceInfoPayload,
    DeviceOrientation,
    DeviceState,
    DeviceStatusPayload,
    MobileButton,
    RunnerTarget,
    ScrollDirection,
    device_info_to_system_info,
)

logger = logging.getLogger(__name__)

LEFT_BUTTON: Final[str] = "left"
# One scroll notch moves the content by this share of the screen.
SCROLL_NOTCH_DISTANCE: Final[float] = 0.1
# Swipes stay this many input units inside the screen edges (an edge start
# would be the OS's back / home / notification gesture).
_EDGE_MARGIN: Final[int] = 8

# Scrolling ``direction`` reveals content that way: the finger moves opposite.
_FINGER_FOR_SCROLL: Final[dict[ScrollDirection, ScrollDirection]] = {
    ScrollDirection.UP: ScrollDirection.DOWN,
    ScrollDirection.DOWN: ScrollDirection.UP,
    ScrollDirection.LEFT: ScrollDirection.RIGHT,
    ScrollDirection.RIGHT: ScrollDirection.LEFT,
}
_UNIT_VECTORS: Final[dict[ScrollDirection, tuple[int, int]]] = {
    ScrollDirection.UP: (0, -1),
    ScrollDirection.DOWN: (0, 1),
    ScrollDirection.LEFT: (-1, 0),
    ScrollDirection.RIGHT: (1, 0),
}
_KEY_ALIASES: Final[dict[str, str]] = {
    "return": "enter",
    "back_space": "backspace",
    "del": "delete",
    "esc": "escape",
    "spacebar": "space",
}
_ORIENTATION_VALUES: Final[dict[DeviceOrientation, str]] = {
    DeviceOrientation.PORTRAIT: "PORTRAIT",
    DeviceOrientation.LANDSCAPE: "LANDSCAPE",
}

_NO_RIGHT_CLICK: Final[str] = (
    "A phone has no right or middle click: use long_press for a context menu"
)
_NO_HOVER: Final[str] = "A phone has no pointer to hover with: tap instead"
_NO_COMBINATIONS: Final[str] = (
    "Key combinations do not exist on a phone: press one key at a time"
)
_SECURE_WINDOW: Final[str] = "The app on the phone blocks screenshots (secure window)"
_RELEASED: Final[str] = "The device was released for the updated Test Runner"
_NOT_STARTED: Final[str] = "Starting the automation session"
_RECONNECTED: Final[str] = "The device is back; restarting the automation session"
_UNAUTHORIZED_HINTS: Final[dict[RunnerTarget, str]] = {
    RunnerTarget.ANDROID: (
        "Allow USB debugging on the phone: accept the prompt (tick Always "
        "allow from this computer)"
    ),
    RunnerTarget.IOS: (
        "Unlock the iPhone or iPad, tap Trust this computer, and turn on "
        "Developer Mode (Settings > Privacy & Security)"
    ),
}


def _detail(text: str) -> str:
    return bounded(text, DEVICE_STATUS_DETAIL_MAX_CHARS)


class MobileBackend:
    def __init__(
        self,
        settings: MobileSettings,
        device: DiscoveredDevice,
        platform: MobilePlatform,
        session: MobileSession,
        server: ManagedAppiumServer,
        policy: MobileAppPolicy,
    ) -> None:
        self._settings = settings
        self._device = device
        self._platform = platform
        self._session = session
        self._server = server
        self._policy = policy
        self._state_lock = threading.Lock()
        self._starting = False
        self._attempted = False
        self._released = False
        self._failure = ""
        self._needs_restart = False
        self._capture: tuple[int, int] | None = None
        self._input: tuple[int, int] | None = None

    # --- DesktopBackend -------------------------------------------------

    @property
    def name(self) -> str:
        return f"appium-{self._platform.target.value}"

    @property
    def target(self) -> RunnerTarget:
        return self._platform.target

    def click(self, x: int, y: int, button: str) -> None:
        if button != LEFT_BUTTON:
            raise ExecutionError(_NO_RIGHT_CLICK)
        self._session.call(lambda driver: gestures.tap(driver, x, y))

    def double_click(self, x: int, y: int) -> None:
        self._session.call(lambda driver: gestures.double_tap(driver, x, y))

    def move_to(self, x: int, y: int) -> None:
        raise ExecutionError(_NO_HOVER)

    def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        duration: float,
    ) -> None:
        if button != LEFT_BUTTON:
            raise ExecutionError(_NO_RIGHT_CLICK)
        self._session.call(
            lambda driver: gestures.drag(
                driver, (start_x, start_y), (end_x, end_y), duration
            )
        )

    def scroll(
        self, direction: ScrollDirection, amount: int, x: int | None, y: int | None
    ) -> None:
        distance = min(max(amount, 1) * SCROLL_NOTCH_DISTANCE, SWIPE_MAX_DISTANCE)
        self.swipe(
            _FINGER_FOR_SCROLL[direction], x, y, distance, DEFAULT_SWIPE_DURATION
        )

    def type_text(self, text: str, interval: float) -> None:
        self._session.call(
            lambda driver: self._platform.type_text(driver, text, interval)
        )

    def resolve_key(self, name: str) -> str | None:
        key = name.strip().lower()
        key = _KEY_ALIASES.get(key, key)
        return key if key in self._platform.supported_keys() else None

    def press_keys(self, keys: Sequence[str]) -> None:
        if len(keys) != 1:
            raise ExecutionError(_NO_COMBINATIONS)
        key = keys[0]
        self._session.call(lambda driver: self._platform.press_key(driver, key))

    def screenshot(self) -> Image.Image:
        png, input_size = self._session.call(self._capture_with_size)
        try:
            image = Image.open(io.BytesIO(png))
            image.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ScreenCaptureError(
                f"The phone sent an unreadable screenshot ({exc})"
            ) from exc
        rgb = image.convert("RGB")
        if rgb.getbbox() is None:
            raise ScreenCaptureError(_SECURE_WINDOW)
        with self._state_lock:
            self._capture = rgb.size
            self._input = input_size
        return rgb

    def screen_size(self) -> tuple[int, int]:
        """Portrait screenshot pixels; (0, 0) before the first screenshot."""
        capture = self._capture
        if capture is None:
            return 0, 0
        return min(capture), max(capture)

    def capture_size(self) -> tuple[int, int]:
        if self._capture is None:
            self.screenshot()
        return self._capture or (0, 0)

    def input_size(self) -> tuple[int, int]:
        if self._input is None:
            self.screenshot()
        return self._input or (0, 0)

    def session_info(self) -> dict[str, str]:
        return device_info_to_system_info(self.device_info())

    def diagnostic_steps(self) -> tuple[DiagnosticStep, ...]:
        return (
            *prerequisite_steps(self._settings),
            *device_steps(lambda: self, interactive=False),
        )

    # --- MobileDevice ---------------------------------------------------

    def device_info(self) -> DeviceInfoPayload:
        device = self._device
        return DeviceInfoPayload(
            target=device.target,
            platform=self._platform.platform_name(device),
            kind=device.kind.value,
            model=device.model,
            name=device.name,
            os_version=device.os_version,
            device_id=device.device_id,
            scale=self._scale(),
        )

    def start(self) -> None:
        with self._state_lock:
            if self._released:
                raise DeviceDisconnectedError(_RELEASED)
            if self._starting:
                return
            self._starting = True
            self._attempted = True
            self._failure = ""
        self._session.mark_starting(True)
        try:
            self._start_session()
        except DeviceDisconnectedError:
            raise
        except ExecutionError as exc:
            logger.error("The phone's automation session failed: %s", exc)
            with self._state_lock:
                self._failure = _detail(str(exc))
        finally:
            self._session.mark_starting(False)
            with self._state_lock:
                self._starting = False

    def stop(self) -> None:
        """Release the device (exit or self-update) until ``reacquire``;
        never raises. A start running meanwhile tears down what it made."""
        with self._state_lock:
            self._released = True
        self._session.close()
        self._server.stop()

    def reacquire(self) -> None:
        """Undo ``stop``: the device monitor starts a new session."""
        with self._state_lock:
            if not self._released:
                return
            self._released = False
            self._failure = ""
            self._needs_restart = True
        logger.info("The device is ours again; the session restarts")

    def status(self) -> DeviceStatusPayload:
        with self._state_lock:
            if self._released:
                return DeviceStatusPayload(DeviceState.DISCONNECTED, _RELEASED)
        found = self._find_self()
        if found is None or found.state is DeviceState.DISCONNECTED:
            with self._state_lock:
                self._needs_restart = self._attempted
            return DeviceStatusPayload(
                DeviceState.DISCONNECTED,
                _detail(
                    f"{self._device.name} ({self._device.device_id}) is not connected"
                ),
            )
        if found.state is DeviceState.UNAUTHORIZED:
            return DeviceStatusPayload(
                DeviceState.UNAUTHORIZED, _UNAUTHORIZED_HINTS[self.target]
            )
        return self._session_status()

    def swipe(
        self,
        direction: ScrollDirection,
        x: int | None,
        y: int | None,
        distance: float,
        duration: float,
    ) -> None:
        start, end = self._swipe_points(direction, x, y, distance)
        self._session.call(lambda driver: gestures.swipe(driver, start, end, duration))

    def long_press(self, x: int, y: int, duration: float) -> None:
        self._session.call(lambda driver: gestures.long_press(driver, x, y, duration))

    def press_button(self, button: MobileButton) -> None:
        self._session.call(lambda driver: self._platform.press_button(driver, button))

    def rotate(self, orientation: DeviceOrientation) -> None:
        value = _ORIENTATION_VALUES[orientation]

        def operation(driver: WebDriver) -> None:
            driver.orientation = value

        self._session.call(operation)
        with self._state_lock:
            # The next pointer mapping takes a fresh screenshot first.
            self._capture = None
            self._input = None

    def hide_keyboard(self) -> bool:
        return self._session.call(_hide_keyboard)

    def open_url(self, url: str) -> None:
        self._session.call(lambda driver: self._platform.open_url(driver, url))

    def install_app(self, path: str) -> str:
        return self._session.call_install(
            lambda driver: self._platform.install_app(driver, path)
        )

    def launch_app(self, app: str) -> str:
        def operation(driver: WebDriver) -> str:
            app_id = self._platform.resolve_app(driver, app)
            driver.activate_app(app_id)
            return app_id

        app_id = self._session.call(operation)
        self._policy.note_launched(app_id)
        return app_id

    def close_app(self, app: str, force: bool) -> str:
        # ``force`` changes nothing: a phone app is always terminated. One
        # call, so resolving and terminating share one time budget.
        def operation(driver: WebDriver) -> tuple[str, str]:
            app_id = self._platform.resolve_app(driver, app)
            if self._policy.is_protected(app_id):
                raise ExecutionError(
                    f"{app_id} is the home screen or part of the test automation "
                    "and is never closed; press home instead"
                )
            return app_id, _terminate(driver, app_id)

        app_id, message = self._session.call(operation)
        self._policy.note_closed(app_id)
        return message

    def is_app_installed(self, app: str) -> str | None:
        return self._session.call(
            lambda driver: self._platform.find_installed_app(driver, app)
        )

    def foreground_app(self) -> str | None:
        return self._session.call(self._platform.current_app)

    def installed_apps(self) -> dict[str, str] | None:
        return self._session.call(self._platform.installed_apps)

    def cleanup(self) -> list[str]:
        """One call: every request fits the call's budget, so the reply
        reaches the server before its cleanup timeout; apps left when the
        budget runs short are reported, not closed."""
        app_ids = self._policy.take_launched()
        # Set before the call: the lock wait and an inline session recreate
        # (which runs the operation again) spend the same budget.
        deadline = time.monotonic() + CLEANUP_BUDGET_SECONDS
        return self._session.call(
            lambda driver: self._cleanup(driver, app_ids, deadline)
        )

    def _cleanup(
        self, driver: WebDriver, app_ids: list[str], deadline: float
    ) -> list[str]:
        # Home first (two requests): it also dismisses the keyboard and
        # records the launcher as protected before any app is closed.
        try:
            self._go_home(driver, deadline)
            lines = ["Went to the home screen"]
        except _CLEANUP_STEP_ERRORS as exc:
            # Still close the launched apps: they are not offered again.
            _reraise_session_loss(exc)
            lines = [f"Could not go to the home screen: {_step_error(exc)}"]
        for app_id in app_ids:
            if not _request_fits(deadline):
                lines.append(f"Left {app_id} open (no time left)")
                continue
            lines.append(self._cleanup_app(driver, app_id))
        return lines

    def web_call[T](self, operation: Callable[[WebDriver], T]) -> T:
        return self._session.call(operation)

    # --- helpers ----------------------------------------------------------

    def _find_self(self) -> DiscoveredDevice | None:
        device = self._device
        return find_device(self.target, device.device_id, device.kind)

    def _start_session(self) -> None:
        found = self._find_self()
        if found is None or found.state is DeviceState.DISCONNECTED:
            raise DeviceDisconnectedError(
                f"{self._device.name} ({self._device.device_id}) is not connected"
            )
        if found.state is DeviceState.UNAUTHORIZED:
            raise ExecutionError(_UNAUTHORIZED_HINTS[self.target])
        url = self._settings.appium_url or self._server.start()
        self._abort_if_released()
        self._session.open(url)
        self._abort_if_released()
        with self._state_lock:
            self._needs_restart = False
        # The session is up: let the first screenshot (and agent actions) in.
        self._session.mark_starting(False)
        try:
            self.screenshot()
        except ExecutionError as exc:
            # Sizes come with the next screenshot; a secure window is not a
            # session failure.
            logger.warning("First screenshot of the phone failed: %s", exc)

    def _abort_if_released(self) -> None:
        """Undo this start's work when ``stop`` ran meanwhile."""
        with self._state_lock:
            released = self._released
        if released:
            self._session.close()
            self._server.stop()
            raise DeviceDisconnectedError(_RELEASED)

    def _session_status(self) -> DeviceStatusPayload:
        with self._state_lock:
            if self._starting or not self._attempted or self._session.recreating:
                return DeviceStatusPayload(DeviceState.STARTING, _NOT_STARTED)
            if self._failure:
                return DeviceStatusPayload(DeviceState.UNAVAILABLE, self._failure)
            if self._needs_restart or not self._session.active:
                return DeviceStatusPayload(DeviceState.UNAVAILABLE, _RECONNECTED)
        return DeviceStatusPayload(DeviceState.READY, "")

    def _capture_with_size(self, driver: WebDriver) -> tuple[bytes, tuple[int, int]]:
        png = driver.get_screenshot_as_png()
        with Image.open(io.BytesIO(png)) as probe:
            capture = probe.size
        return png, self._input_for(driver, capture)

    def _input_for(
        self, driver: WebDriver, capture: tuple[int, int]
    ) -> tuple[int, int]:
        """The input size, asked again only when the screenshot size changed
        (``rotate`` clears it too)."""
        with self._state_lock:
            known_capture, known_input = self._capture, self._input
        if known_capture == capture and known_input is not None:
            return known_input
        return self._platform.input_size(driver, capture)

    def _scale(self) -> str:
        capture, input_size = self._capture, self._input
        if capture is None or input_size is None or input_size[0] <= 0:
            return ""
        return f"{capture[0] / input_size[0]:.2g}"

    def _swipe_points(
        self, direction: ScrollDirection, x: int | None, y: int | None, distance: float
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        width, height = self.input_size()
        if width <= 0 or height <= 0:
            raise ExecutionError("The phone's screen size is not known yet")
        dx, dy = _UNIT_VECTORS[direction]
        length = round(distance * (width if dx else height))
        if x is None or y is None:
            # Centre the whole swipe on the screen.
            x = width // 2 - dx * length // 2
            y = height // 2 - dy * length // 2
        start = (_clamp(x, width), _clamp(y, height))
        end = (
            _clamp(start[0] + dx * length, width),
            _clamp(start[1] + dy * length, height),
        )
        return start, end

    def _cleanup_app(self, driver: WebDriver, app_id: str) -> str:
        if self._policy.is_protected(app_id):
            return f"Left {app_id} open (never closed)"
        try:
            return _terminate(driver, app_id)
        except _CLEANUP_STEP_ERRORS as exc:
            _reraise_session_loss(exc)
            return f"Could not close {app_id}: {_step_error(exc)}"

    def _go_home(self, driver: WebDriver, deadline: float) -> None:
        self._platform.press_button(driver, MobileButton.HOME)
        if not _request_fits(deadline):
            return
        # Whatever is in front now is the launcher: never close it.
        launcher = self._platform.current_app(driver)
        if launcher is not None:
            self._policy.add_launcher(launcher)


def _clamp(value: int, size: int) -> int:
    return max(_EDGE_MARGIN, min(size - 1 - _EDGE_MARGIN, value))


def _hide_keyboard(driver: WebDriver) -> bool:
    if not driver.is_keyboard_shown():
        return False
    driver.hide_keyboard()
    return True


# One cleanup step failing (driver error, read timeout, socket error) is a
# line; a lost session is re-raised so the session recreates it.
_CLEANUP_STEP_ERRORS: Final = (WebDriverException, Urllib3HTTPError, OSError)


def _request_fits(deadline: float) -> bool:
    """Whether one more device request (at most COMMAND_HTTP_TIMEOUT_SECONDS)
    finishes before ``deadline``."""
    return time.monotonic() + COMMAND_HTTP_TIMEOUT_SECONDS <= deadline


def _step_error(exc: Exception) -> str:
    if isinstance(exc, WebDriverException):
        return bounded(str(exc.msg or exc))
    return bounded(str(exc) or type(exc).__name__)


def _reraise_session_loss(exc: Exception) -> None:
    """A lost session must reach the session's recreate path, not a line."""
    if isinstance(exc, (InvalidSessionIdException, NoSuchDriverException)):
        raise exc


def _terminate(driver: WebDriver, app_id: str) -> str:
    if driver.terminate_app(app_id):
        return f"Closed {app_id}"
    return f"{app_id} was not running"
