"""The one Appium session a phone runner holds, and its error translation.

``open`` creates the session (blocking; a first WebDriverAgent build takes
minutes), ``call`` runs one driver operation under a lock, so an agent
action, a Manual capture and a readiness check never interleave. A device
that is gone, or a session not created yet, raises DeviceDisconnectedError;
an invalid session is recreated once and the operation retried.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
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
from urllib3.exceptions import ReadTimeoutError

from controller_client.exceptions import DeviceDisconnectedError, ExecutionError
from controller_client.mobile_appium_server import (
    REQUIRED_DRIVER,
    driver_install_hint,
    free_local_port,
)
from controller_client.mobile_discovery import DiscoveredDevice
from controller_client.mobile_platform import MobilePlatform, SessionPorts
from controller_client.protocol import DeviceKind, RunnerTarget

logger = logging.getLogger(__name__)

NEW_COMMAND_TIMEOUT_SECONDS: Final[int] = 3600
# Session creation includes a WebDriverAgent build on a real iPhone.
_SESSION_CREATE_HTTP_TIMEOUT_SECONDS: Final[int] = 900
_COMMAND_HTTP_TIMEOUT_SECONDS: Final[int] = 120
_LOCK_WAIT_SECONDS: Final[float] = 60.0
_ERROR_MAX_CHARS: Final[int] = 400

STILL_STARTING: Final[str] = (
    "The phone's automation session is still starting (a first start on an "
    "iPhone can take several minutes); try again shortly."
)
_NO_SESSION: Final[str] = (
    "There is no automation session with the phone: it is disconnected or "
    "Appium failed to start (see the Test Runner log)."
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


class MobileSession:
    def __init__(self, device: DiscoveredDevice, platform: MobilePlatform) -> None:
        self._device = device
        self._platform = platform
        self._lock = threading.Lock()
        self._driver: WebDriver | None = None
        self._server_url = ""
        self._opening = False

    @property
    def active(self) -> bool:
        return self._driver is not None

    @property
    def opening(self) -> bool:
        return self._opening

    def open(self, server_url: str) -> None:
        """Create a new session on ``server_url``, replacing any old one.

        Raises DeviceDisconnectedError when the device is gone, else
        ExecutionError with the reason and the fix.
        """
        self._opening = True
        try:
            self.close()
            driver = self._create(server_url)
            with self._lock:
                self._driver = driver
                self._server_url = server_url
        finally:
            self._opening = False

    def close(self) -> None:
        """Quit the session; never raises."""
        with self._lock:
            driver, self._driver = self._driver, None
        if driver is not None:
            _quit(driver)

    def call[T](self, operation: Callable[[WebDriver], T]) -> T:
        """Run ``operation`` on the session with errors translated."""
        if not self._lock.acquire(timeout=_LOCK_WAIT_SECONDS):
            raise ExecutionError("The phone is busy with another action; try again")
        try:
            return self._call_locked(operation)
        finally:
            self._lock.release()

    def _call_locked[T](self, operation: Callable[[WebDriver], T]) -> T:
        driver = self._require_driver()
        try:
            return operation(driver)
        except (InvalidSessionIdException, NoSuchDriverException) as exc:
            logger.warning("Appium session lost (%s); recreating it", _error_text(exc))
        except ExecutionError:
            raise
        except ReadTimeoutError as exc:
            raise DeviceDisconnectedError(
                f"The phone did not answer within {_COMMAND_HTTP_TIMEOUT_SECONDS} s"
            ) from exc
        except (WebDriverException, Urllib3HTTPError, OSError) as exc:
            raise self._translate(exc) from exc
        driver = self._recreate_locked()
        try:
            return operation(driver)
        except ExecutionError:
            raise
        except (WebDriverException, Urllib3HTTPError, OSError) as exc:
            raise self._translate(exc) from exc

    def _require_driver(self) -> WebDriver:
        if self._driver is not None:
            return self._driver
        raise DeviceDisconnectedError(STILL_STARTING if self._opening else _NO_SESSION)

    def _recreate_locked(self) -> WebDriver:
        old, self._driver = self._driver, None
        if old is not None:
            _quit(old)
        if not self._server_url:
            raise DeviceDisconnectedError(_NO_SESSION)
        try:
            self._driver = self._create(self._server_url)
        except ExecutionError as exc:
            raise DeviceDisconnectedError(
                f"The phone's session was lost and could not be recreated: {exc}"
            ) from exc
        return self._driver

    def _translate(self, exc: Exception) -> ExecutionError:
        text = bounded(_error_text(exc))
        if _is_device_gone(exc):
            return DeviceDisconnectedError(f"The phone is not reachable: {text}")
        return ExecutionError(text)

    def _create(self, server_url: str) -> WebDriver:
        options = self._options()
        config = AppiumClientConfig(
            remote_server_addr=server_url,
            timeout=_SESSION_CREATE_HTTP_TIMEOUT_SECONDS,
        )
        logger.info(
            "Creating the Appium session with %s %s",
            self._device.name,
            self._device.device_id,
        )
        try:
            driver = WebDriver(server_url, options=options, client_config=config)
        except (WebDriverException, Urllib3HTTPError, OSError) as exc:
            raise self._creation_error(exc) from exc
        # Later commands answer within seconds; never hold the lock for 15 min.
        config.timeout = _COMMAND_HTTP_TIMEOUT_SECONDS
        return driver

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
