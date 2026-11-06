"""The one Appium session a phone runner holds, and its error translation.

``open`` creates the session (blocking; a first WebDriverAgent build takes
minutes) without the action lock; ``call`` runs one driver operation under
that lock, so an agent action, a Manual capture and a readiness check never
interleave. Every call answers within a budget under the server's reply
timeout: a busy lock, a timed-out reply or a session lost mid-action becomes
an error, never a long wait. A device that is gone, or a session not created
yet (or still starting), raises DeviceDisconnectedError; an invalid session
is recreated once within the budget left and the operation retried.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from appium.options.android import UiAutomator2Options
from appium.options.common.base import AppiumOptions
from appium.options.ios import XCUITestOptions
from appium.webdriver.client_config import AppiumClientConfig
from appium.webdriver.webdriver import WebDriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchDriverException,
    SessionNotCreatedException,
    WebDriverException,
)
from urllib3.exceptions import HTTPError as Urllib3HTTPError
from urllib3.exceptions import MaxRetryError, ReadTimeoutError
from urllib3.util.retry import Retry

from controller_client.exceptions import DeviceDisconnectedError, ExecutionError
from controller_client.mobile_appium_server import (
    REQUIRED_DRIVER,
    driver_install_hint,
    free_local_port,
)
from controller_client.mobile_discovery import DiscoveredDevice, find_device
from controller_client.mobile_platform import MobilePlatform, SessionPorts
from controller_client.protocol import DeviceKind, DeviceState, RunnerTarget

logger = logging.getLogger(__name__)

NEW_COMMAND_TIMEOUT_SECONDS: Final[int] = 3600
# Session creation includes a WebDriverAgent build on a real iPhone.
_SESSION_CREATE_HTTP_TIMEOUT_SECONDS: Final[int] = 900
# The server waits 30 s for a phone action's reply (install_app: 300 s), so
# every call, lock wait included, answers within its budget: a late reply
# would read as "Test Runner stopped responding".
_ACTION_BUDGET_SECONDS: Final[float] = 24.0
_INSTALL_BUDGET_SECONDS: Final[float] = 270.0
_COMMAND_HTTP_TIMEOUT_SECONDS: Final[int] = 20
INSTALL_HTTP_TIMEOUT_SECONDS: Final[int] = 240
# A session lost mid-action is recreated inline only within the budget left;
# otherwise the device monitor restarts it with the full creation timeout.
_RECREATE_HTTP_TIMEOUT_SECONDS: Final[int] = 15
_MIN_PHASE_SECONDS: Final[int] = 2
_LOCK_WAIT_SECONDS: Final[float] = 10.0
_ERROR_MAX_CHARS: Final[int] = 400
# No urllib3 retry of a timed-out read: a screenshot (GET) would otherwise
# wait four times the timeout.
_POOL_ARGS: Final[dict[str, object]] = {
    "init_args_for_pool_manager": {"retries": Retry(read=0)}
}

STILL_STARTING: Final[str] = (
    "The phone's automation session is still starting (a first start on an "
    "iPhone can take several minutes); try again shortly."
)
_NO_SESSION: Final[str] = (
    "There is no automation session with the phone: it is disconnected or "
    "Appium failed to start (see the Test Runner log)."
)
_BUSY: Final[str] = "The phone is busy with another action; try again"
_OUT_OF_TIME: Final[str] = (
    "The phone's automation session was just recreated and the action ran out "
    "of time; try it again"
)
# Lower-case fragments of driver errors meaning the device itself is gone.
_DEVICE_GONE_MARKERS: Final[tuple[str, ...]] = (
    "device offline",
    "device not found",
    "is not connected",
    "has been disconnected",
    "no devices/emulators found",
    "could not find a connected",
    "instrumentation process is not running",
    "socket hang up",
    "econnrefused",
    "econnreset",
    "could not proxy command",
    "unable to connect",
)
_WDA_BUILD_HINT: Final[str] = (
    " WebDriverAgent could not be built or signed for this iPhone: set "
    "CONTROLLER_IOS_TEAM_ID (and CONTROLLER_IOS_SIGNING_ID) to your Apple team, "
    "or install WDA yourself and set CONTROLLER_IOS_PREINSTALLED_WDA=true with "
    "CONTROLLER_IOS_WDA_BUNDLE_ID. Run python -m controller_client.diagnose "
    "--mobile for details."
)


def bounded(text: str, limit: int = _ERROR_MAX_CHARS) -> str:
    """One line, at most ``limit`` characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _error_text(exc: Exception) -> str:
    if isinstance(exc, WebDriverException):
        return exc.msg or type(exc).__name__
    return str(exc) or type(exc).__name__


def _is_device_gone(exc: Exception) -> bool:
    if isinstance(exc, (Urllib3HTTPError, OSError)):
        return True
    message = _error_text(exc).lower()
    return any(marker in message for marker in _DEVICE_GONE_MARKERS)


def _is_read_timeout(exc: Exception) -> bool:
    if isinstance(exc, (ReadTimeoutError, TimeoutError)):
        return True
    return isinstance(exc, MaxRetryError) and isinstance(exc.reason, ReadTimeoutError)


@dataclass(frozen=True)
class _CallLimits:
    budget_seconds: float
    http_timeout_seconds: int
    # Install: a timeout is a plain failure. Other actions: the device is
    # looked up, and only a device that is gone reads as disconnected.
    timeout_is_failure: bool


_ACTION_LIMITS: Final[_CallLimits] = _CallLimits(
    _ACTION_BUDGET_SECONDS, _COMMAND_HTTP_TIMEOUT_SECONDS, timeout_is_failure=False
)
_INSTALL_LIMITS: Final[_CallLimits] = _CallLimits(
    _INSTALL_BUDGET_SECONDS, INSTALL_HTTP_TIMEOUT_SECONDS, timeout_is_failure=True
)
_CALL_ERRORS: Final = (WebDriverException, Urllib3HTTPError, OSError)


class MobileSession:
    def __init__(self, device: DiscoveredDevice, platform: MobilePlatform) -> None:
        self._device = device
        self._platform = platform
        self._lock = threading.Lock()
        self._driver: WebDriver | None = None
        self._config: AppiumClientConfig | None = None
        self._server_url = ""
        self._starting = False

    @property
    def active(self) -> bool:
        return self._driver is not None

    def mark_starting(self, starting: bool) -> None:
        """Set while the backend (re)starts Appium and the session: calls
        then answer "still starting" at once instead of waiting."""
        self._starting = starting

    def open(self, server_url: str) -> None:
        """Create a new session on ``server_url``, replacing any old one.

        Raises DeviceDisconnectedError when the device is gone, else
        ExecutionError with the reason and the fix.
        """
        self.close()
        driver, config = self._create(server_url, _SESSION_CREATE_HTTP_TIMEOUT_SECONDS)
        with self._lock:
            self._driver = driver
            self._config = config
            self._server_url = server_url

    def close(self) -> None:
        """Quit the session; never raises."""
        with self._lock:
            driver, self._driver = self._driver, None
            self._config = None
        if driver is not None:
            _quit(driver)

    def call[T](self, operation: Callable[[WebDriver], T]) -> T:
        """Run ``operation`` on the session with errors translated, within
        the ordinary action budget."""
        return self._run(operation, _ACTION_LIMITS)

    def call_install[T](self, operation: Callable[[WebDriver], T]) -> T:
        """Like ``call`` with the longer app-install timeout; a timeout is a
        plain ExecutionError."""
        return self._run(operation, _INSTALL_LIMITS)

    def _run[T](self, operation: Callable[[WebDriver], T], limits: _CallLimits) -> T:
        if self._starting:
            raise DeviceDisconnectedError(STILL_STARTING)
        deadline = time.monotonic() + limits.budget_seconds
        if not self._lock.acquire(timeout=_LOCK_WAIT_SECONDS):
            raise ExecutionError(_BUSY)
        try:
            return self._call_locked(operation, limits, deadline)
        finally:
            self._lock.release()

    def _call_locked[T](
        self, operation: Callable[[WebDriver], T], limits: _CallLimits, deadline: float
    ) -> T:
        driver = self._require_driver()
        self._apply_timeout(limits.http_timeout_seconds, deadline)
        try:
            return operation(driver)
        except (InvalidSessionIdException, NoSuchDriverException) as exc:
            logger.warning("Appium session lost (%s); recreating it", _error_text(exc))
        except ExecutionError:
            raise
        except _CALL_ERRORS as exc:
            raise self._translate(exc, limits) from exc
        driver = self._recreate_locked(deadline)
        self._apply_timeout(limits.http_timeout_seconds, deadline)
        try:
            return operation(driver)
        except ExecutionError:
            raise
        except _CALL_ERRORS as exc:
            raise self._translate(exc, limits) from exc

    def _require_driver(self) -> WebDriver:
        if self._driver is not None:
            return self._driver
        raise DeviceDisconnectedError(STILL_STARTING if self._starting else _NO_SESSION)

    def _apply_timeout(self, limit: int, deadline: float) -> None:
        """Bound the next HTTP replies by ``limit`` and the budget left."""
        seconds = min(limit, int(deadline - time.monotonic()))
        if seconds < _MIN_PHASE_SECONDS:
            raise ExecutionError(_OUT_OF_TIME)
        if self._config is not None:
            self._config.timeout = seconds

    def _recreate_locked(self, deadline: float) -> WebDriver:
        """A new session within the budget left; DeviceDisconnectedError (the
        device monitor then restarts it) when that is not possible."""
        old, self._driver = self._driver, None
        self._config = None
        if old is not None:
            _quit(old)
        seconds = min(_RECREATE_HTTP_TIMEOUT_SECONDS, int(deadline - time.monotonic()))
        if not self._server_url or seconds < _MIN_PHASE_SECONDS:
            raise DeviceDisconnectedError(_NO_SESSION)
        try:
            driver, config = self._create(self._server_url, seconds)
        except ExecutionError as exc:
            raise DeviceDisconnectedError(
                "The phone's session was lost and could not be recreated "
                f"(the Test Runner keeps trying): {exc}"
            ) from exc
        self._driver, self._config = driver, config
        return driver

    def _translate(self, exc: Exception, limits: _CallLimits) -> ExecutionError:
        if _is_read_timeout(exc):
            return self._timeout_error(limits)
        text = bounded(_error_text(exc))
        if _is_device_gone(exc):
            return DeviceDisconnectedError(f"The phone is not reachable: {text}")
        return ExecutionError(text)

    def _timeout_error(self, limits: _CallLimits) -> ExecutionError:
        if limits.timeout_is_failure:
            return ExecutionError(
                "Installing the app did not finish within "
                f"{limits.http_timeout_seconds} s"
            )
        device = self._device
        found = find_device(device.target, device.device_id, device.kind)
        if found is None or found.state is DeviceState.DISCONNECTED:
            return DeviceDisconnectedError(
                "The phone did not answer and is no longer connected"
            )
        return ExecutionError(
            "The phone did not answer in time (it is still connected); try again"
        )

    def _create(
        self, server_url: str, timeout_seconds: int
    ) -> tuple[WebDriver, AppiumClientConfig]:
        options = self._options()
        config = AppiumClientConfig(
            remote_server_addr=server_url,
            timeout=timeout_seconds,
            init_args_for_pool_manager=_POOL_ARGS,
        )
        logger.info(
            "Creating the Appium session with %s %s",
            self._device.name,
            self._device.device_id,
        )
        try:
            driver = WebDriver(server_url, options=options, client_config=config)
        except _CALL_ERRORS as exc:
            if _is_read_timeout(exc):
                raise ExecutionError(
                    f"Appium did not create the session within {timeout_seconds} s"
                ) from exc
            raise self._creation_error(exc) from exc
        # Later commands answer within seconds: ``call`` sets each timeout.
        config.timeout = _COMMAND_HTTP_TIMEOUT_SECONDS
        return driver, config

    def _creation_error(self, exc: Exception) -> ExecutionError:
        text = bounded(_error_text(exc))
        lowered = text.lower()
        if "could not find a driver" in lowered:
            driver = REQUIRED_DRIVER[self._platform.target]
            return ExecutionError(f"{text} {driver_install_hint(driver)}")
        if _is_device_gone(exc) and not isinstance(exc, SessionNotCreatedException):
            return DeviceDisconnectedError(f"The phone is not reachable: {text}")
        if (
            self._platform.target is RunnerTarget.IOS
            and self._device.kind is DeviceKind.PHYSICAL
            and ("xcodebuild" in lowered or "webdriveragent" in lowered)
        ):
            return ExecutionError(bounded(text, 200) + _WDA_BUILD_HINT)
        return ExecutionError(f"The Appium session could not be created: {text}")

    def _options(self) -> AppiumOptions:
        options: AppiumOptions = (
            UiAutomator2Options()
            if self._platform.target is RunnerTarget.ANDROID
            else XCUITestOptions()
        )
        driver_port = free_local_port()
        mjpeg_port = free_local_port()
        while mjpeg_port == driver_port:
            mjpeg_port = free_local_port()
        ports = SessionPorts(driver_port=driver_port, mjpeg_server_port=mjpeg_port)
        options.set_capability("udid", self._device.device_id)
        options.set_capability("noReset", True)
        options.set_capability("newCommandTimeout", NEW_COMMAND_TIMEOUT_SECONDS)
        for name, value in self._platform.capabilities(self._device, ports).items():
            options.set_capability(name, value)
        return options


def _quit(driver: WebDriver) -> None:
    try:
        driver.quit()
    except Exception as exc:  # the session or device may already be gone
        logger.debug("Quitting the Appium session failed: %s", exc)
