"""Android browsers of the web browser layer: Chrome, Edge and Firefox.

Page mode talks WebDriver to a vendor driver the runner starts on the host
for the runner's device (``androidDeviceSerial``); the native UiAutomator2
session is never touched in page mode.

- Chrome and Edge: chromedriver with ``androidPackage`` and
  ``androidUseRunningApp``, so it attaches to the running app through its
  DevTools socket and the person's tabs and logins stay. Opening starts the
  app with ``am start`` (a VIEW intent for the URL, aimed at the package)
  and attaches. ``close`` only detaches: the chromedriver process is killed
  without a WebDriver quit, because chromedriver's quit force-stops the app
  on Android (unverified for ``androidUseRunningApp``; killing the driver
  is safe either way).
- Edge without a DevTools socket: accessibility mode through the phone's
  Appium session (``web_browser_accessibility``).
- Firefox: geckodriver with ``moz:firefoxOptions.androidPackage``; it
  starts Firefox itself with its test profile, so only ``web_browser_open``
  makes it controllable and ``close`` ends the session (and Firefox).

Time: every call answers within the hub's phone budget. Sessions get a
page-load and script timeout below the per-command HTTP timeout, so a slow
page ends in the driver's own timeout, not a read timeout. A lazy attach
that leaves too little of the budget for the command answers a retryable
error (the next call reuses the session). A read timeout is never a lost
session, and nothing is retried: a navigate never runs twice. chromedriver
adds ``adb forward`` entries it removes only on quit; the backend detaches
without a quit, so it removes the forwards it saw chromedriver add.

Drivers: Selenium Manager resolves chromedriver at the major version of the
app on the device (read with ``dumpsys package``) and geckodriver at its
latest; ``--offline`` (the flag form of ``SE_OFFLINE``) when driver
downloads are off; else the driver on PATH. Selenium Manager is asked
directly (not through ``webdriver.Chrome``) because its normal path also
requires a desktop browser binary, which an Android host may not have.

Nothing here has run on a device yet; assumptions are marked "Unverified".
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from appium.webdriver.webdriver import WebDriver as AppiumDriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchDriverException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection
from selenium.webdriver.common.options import ArgOptions
from selenium.webdriver.common.proxy import Proxy, ProxyType
from selenium.webdriver.common.selenium_manager import SeleniumManager
from selenium.webdriver.common.service import Service
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.remote_connection import FirefoxRemoteConnection
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.remote.client_config import ClientConfig
from selenium.webdriver.remote.remote_connection import RemoteConnection
from selenium.webdriver.remote.webdriver import WebDriver
from urllib3.exceptions import HTTPError as Urllib3HTTPError
from urllib3.exceptions import MaxRetryError, NewConnectionError, ReadTimeoutError
from urllib3.util.retry import Retry

from controller_client import web_browser_accessibility as a11y
from controller_client.exceptions import (
    ExecutionError,
    WebBrowserNotOpenError,
    WebBrowserSetupNeededError,
    WebBrowserUnsupportedError,
)
from controller_client.mobile_device import MobileDevice
from controller_client.protocol import (
    WEB_BROWSER_OPEN_BUDGET_SECONDS,
    WEB_BROWSER_PHONE_ACTION_BUDGET_SECONDS,
    WebActKind,
    WebBrowser,
    WebBrowserHowToOpen,
    WebBrowserMode,
    WebBrowserSetupStep,
    WebElementTarget,
    WebHistoryAction,
    WebReadWhat,
    WebTabsAction,
    WebWaitState,
    web_url_is_file,
)
from controller_client.web_browser_backend import (
    WebActRequest,
    WebBrowserBackend,
    WebBrowserContext,
    WebEvalResult,
    WebOpenResult,
    WebReadRequest,
)
from controller_client.web_browser_evaluate import (
    WEB_SCRIPT_TIMEOUT_SECONDS,
    evaluate_script,
)
from controller_client.web_browser_page_ops import (
    PageListing,
    page_act,
    page_elements,
    page_history,
    page_navigate,
    page_read,
    page_tabs,
    page_wait,
)
from controller_client.web_browser_presence_mobile import (
    AdbForward,
    AndroidPresence,
    AndroidSnapshot,
    adb_forwards,
    adb_remove_forward,
    android_app_version,
    android_start_app,
)

logger = logging.getLogger(__name__)

CHROME_PACKAGE: Final[str] = "com.android.chrome"
EDGE_PACKAGE: Final[str] = "com.microsoft.emmx"
FIREFOX_PACKAGE: Final[str] = "org.mozilla.firefox"
_PACKAGES: Final[tuple[str, ...]] = (CHROME_PACKAGE, EDGE_PACKAGE, FIREFOX_PACKAGE)
# Chrome's DevTools socket; other Chromium builds use "<package>_devtools_remote"
# (Unverified for Edge, which may also use Chrome's name).
_CHROME_SOCKET: Final[str] = "chrome_devtools_remote"
_OWN_SOCKET_SUFFIX: Final[str] = "_devtools_remote"

# After ``am start``: the process appears, then the DevTools socket.
_START_WAIT_SECONDS: Final[float] = 15.0
_SOCKET_AFTER_START_SECONDS: Final[float] = 5.0
_POLL_SECONDS: Final[float] = 0.5
# Every call ends this much before the hub's budget, so its reply is sent
# before the hub gives up on it.
_BUDGET_MARGIN_SECONDS: Final[float] = 1.0
_ACTION_BUDGET_SECONDS: Final[float] = (
    WEB_BROWSER_PHONE_ACTION_BUDGET_SECONDS - _BUDGET_MARGIN_SECONDS
)
_OPEN_BUDGET_SECONDS: Final[float] = (
    WEB_BROWSER_OPEN_BUDGET_SECONDS - _BUDGET_MARGIN_SECONDS
)
# Mirrors the phone session's own call budget (``mobile_session``): an
# accessibility call starts only when that much of the budget is left.
_PHONE_SESSION_BUDGET_SECONDS: Final[float] = 24.0
# Session creation, capped by the budget left: ``web_browser_open`` has the
# long budget; an attach on a later call (a browser the person opened) gets
# the action budget.
_OPEN_CREATE_TIMEOUT_SECONDS: Final[int] = 45
_ATTACH_CREATE_TIMEOUT_SECONDS: Final[int] = 15
_FIREFOX_CREATE_TIMEOUT_SECONDS: Final[int] = 50
_MIN_CREATE_SECONDS: Final[int] = 3
# The driver's own page-load and script timeouts end before the HTTP read
# timeout of one command, so a slow page never reads as a lost session.
_PAGE_LOAD_TIMEOUT_SECONDS: Final[int] = 15
_COMMAND_TIMEOUT_SECONDS: Final[int] = 18
# No urllib3 retry of a timed-out read (a GET would wait several times).
_POOL_ARGS: Final[dict[str, object]] = {
    "init_args_for_pool_manager": {"retries": Retry(read=0)}
}
_TCP_FORWARD_RE: Final[re.Pattern[str]] = re.compile(r"tcp:\d{1,5}")
_VERSION_RECHECK_SECONDS: Final[float] = 10.0
_SELENIUM_MANAGER_NETWORK_TIMEOUT: Final[str] = "30"
# Cleanup and shutdown end within a few seconds: a short quit, then the
# driver process is stopped here (never Selenium's ``service.stop``, which
# can wait up to a minute).
_QUIT_TIMEOUT_SECONDS: Final[int] = 4
_PROCESS_WAIT_SECONDS: Final[float] = 1.0

_NOT_STARTED: Final[str] = "The browser session could not be started"
_SLOW_START: Final[str] = "The browser did not start in time; try again"
_PAGE_ONLY: Final[str] = "Not available in accessibility mode"
_NO_HOVER: Final[str] = "A phone has no pointer to hover with"
_NO_FILE_URLS: Final[str] = "File URLs cannot be opened on a phone"
_UNKNOWN_KEY: Final[str] = "The key cannot be pressed on the phone"
_DETACHED: Final[str] = "Detached from the browser; the app stays open"
_CLOSED: Final[str] = "Closed the browser"
_ATTACHED_RETRY: Final[str] = "Connected to the browser; try the action again"
_OUT_OF_TIME: Final[str] = "The browser did not answer in time; try again"
_PHONE_SLOW: Final[str] = "The phone answered slowly; try again"
_LOST: Final[str] = "The connection to the browser was lost; try again"
_FAILED: Final[str] = "The browser could not carry out the action"

# Lower-case fragments of driver errors.
_SESSION_LOST_MARKERS: Final[tuple[str, ...]] = (
    "not reachable",
    "disconnected",
    "no such session",
    "session deleted",
    "invalid session id",
)
_DRIVER_MISMATCH_MARKERS: Final[tuple[str, ...]] = (
    "only supports chrome version",
    "this version of chromedriver",
)
_DEVTOOLS_MARKERS: Final[tuple[str, ...]] = (
    "devtools",
    "unable to discover open pages",
    "socket",
)
# Unverified: geckodriver cannot reach Marionette when Firefox's "Remote
# debugging via USB" setting is off (Firefox then ignores the config file
# geckodriver pushes) and reports a connection failure or a timeout.
_FIREFOX_REMOTE_OFF_MARKERS: Final[tuple[str, ...]] = (
    "marionette",
    "timed out",
    "connection refused",
    "failed to connect",
)
_ANDROID_KEYCODES: Final[dict[str, int]] = {
    "enter": 66,
    "return": 66,
    "tab": 61,
    "backspace": 67,
    "delete": 112,
    "space": 62,
    "escape": 111,
    "esc": 111,
}
_CALL_ERRORS: Final = (WebDriverException, Urllib3HTTPError, OSError)


def make_android_backends(
    context: WebBrowserContext,
) -> dict[WebBrowser, WebBrowserBackend]:
    presence = AndroidPresence(context.device_serial, _PACKAGES)
    resolver = _DriverResolver(download=context.driver_download)
    return {
        WebBrowser.CHROME: AndroidChromiumBackend(
            WebBrowser.CHROME, CHROME_PACKAGE, context, presence, resolver
        ),
        WebBrowser.EDGE: AndroidChromiumBackend(
            WebBrowser.EDGE, EDGE_PACKAGE, context, presence, resolver
        ),
        WebBrowser.FIREFOX: AndroidFirefoxBackend(context, presence, resolver),
    }


# --- Drivers ---------------------------------------------------------------


class _DriverResolver:
    """Driver paths from Selenium Manager (cached per driver and version),
    else PATH, else WebBrowserSetupNeededError(DRIVER_UNAVAILABLE)."""

    def __init__(self, download: bool) -> None:
        self._download = download
        self._paths: dict[tuple[str, str], str] = {}

    def resolve(self, browser_name: str, driver_name: str, major: str) -> str:
        key = (driver_name, major)
        cached = self._paths.get(key)
        if cached is not None and Path(cached).is_file():
            return cached
        path = self._from_selenium_manager(browser_name, driver_name, major)
        if path is None:
            path = shutil.which(driver_name)
        if path is None:
            raise WebBrowserSetupNeededError(WebBrowserSetupStep.DRIVER_UNAVAILABLE)
        self._paths[key] = path
        return path

    def _from_selenium_manager(
        self, browser_name: str, driver_name: str, major: str
    ) -> str | None:
        # Unverified: with --browser-version and --avoid-browser-download,
        # Selenium Manager resolves the driver for that major version even
        # when no desktop browser of that version is installed.
        args = [
            "--browser",
            browser_name,
            "--driver",
            driver_name,
            "--avoid-browser-download",
            "--timeout",
            _SELENIUM_MANAGER_NETWORK_TIMEOUT,
        ]
        if major:
            args += ["--browser-version", major]
        if not self._download:
            args.append("--offline")
        try:
            result = SeleniumManager().binary_paths(args)
        except WebDriverException as exc:
            logger.info("Selenium Manager found no %s: %s", driver_name, exc.msg)
            return None
        path = result.get("driver_path") if isinstance(result, dict) else None
        if isinstance(path, str) and path and Path(path).is_file():
            return path
        return None


def _major(version: str) -> str:
    head = version.split(".", 1)[0].strip()
    return head if head.isdigit() else ""


@dataclass
class _DriverSession:
    driver: WebDriver
    service: Service
    config: ClientConfig
    # ``adb forward`` locals ("tcp:N") chromedriver added for this session.
    forwards: tuple[str, ...] = ()


def _loopback_config(url: str, timeout: int) -> ClientConfig:
    return ClientConfig(
        remote_server_addr=url,
        timeout=timeout,
        proxy=Proxy(raw={"proxyType": ProxyType.DIRECT}),
        init_args_for_pool_manager=_POOL_ARGS,
    )


def _seconds_left(deadline: float) -> int:
    return int(deadline - time.monotonic())


def _start_session(
    service: Service,
    executor_for: Callable[[ClientConfig], RemoteConnection],
    options: ArgOptions,
    timeout: int,
) -> _DriverSession:
    """Start the driver process and a session on it with the page-load and
    script timeouts set; the process is killed when that fails."""
    service.start()
    config = _loopback_config(service.service_url, timeout)
    try:
        driver = WebDriver(command_executor=executor_for(config), options=options)
        config.timeout = _COMMAND_TIMEOUT_SECONDS
        driver.set_page_load_timeout(_PAGE_LOAD_TIMEOUT_SECONDS)
        driver.set_script_timeout(WEB_SCRIPT_TIMEOUT_SECONDS)
    except BaseException:
        _kill(service)
        raise
    return _DriverSession(driver, service, config)


def _create_timeout(limit: int, deadline: float) -> int:
    """``limit`` capped by the budget left; a retryable error when too
    little is left to start a session."""
    seconds = min(limit, _seconds_left(deadline))
    if seconds < _MIN_CREATE_SECONDS:
        raise ExecutionError(_OUT_OF_TIME)
    return seconds


def _require_command_time(deadline: float) -> None:
    """One command needs its full HTTP timeout within the budget left."""
    if _seconds_left(deadline) < _COMMAND_TIMEOUT_SECONDS:
        raise ExecutionError(_ATTACHED_RETRY)


def _new_forwards(
    before: frozenset[AdbForward] | None, serial: str | None, socket: str
) -> tuple[str, ...]:
    """The tcp forwards to ``socket`` that appeared since ``before``.
    Unverified: chromedriver forwards ``tcp:N`` to ``localabstract:<socket>``."""
    if before is None:
        return ()
    after = adb_forwards(serial)
    if after is None:
        return ()
    remote = f"localabstract:{socket}"
    return tuple(
        sorted(
            forward.local
            for forward in after - before
            if forward.remote == remote and _TCP_FORWARD_RE.fullmatch(forward.local)
        )
    )


def _remove_forwards(serial: str | None, forwards: tuple[str, ...]) -> None:
    for local in forwards:
        adb_remove_forward(serial, local)


def _kill(service: Service) -> None:
    """End the driver process at once, without a WebDriver quit (chromedriver
    on detach); never raises. At most ``_PROCESS_WAIT_SECONDS``."""
    process = getattr(service, "process", None)
    if not isinstance(process, subprocess.Popen):
        return
    try:
        if process.poll() is None:
            process.kill()
            process.wait(_PROCESS_WAIT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Ending the browser driver failed: %s", exc)


def _stop(service: Service) -> None:
    """Terminate the driver process, wait briefly, then kill it; never
    raises. At most twice ``_PROCESS_WAIT_SECONDS``."""
    process = getattr(service, "process", None)
    if not isinstance(process, subprocess.Popen):
        return
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(_PROCESS_WAIT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Stopping the browser driver failed: %s", exc)
    _kill(service)


def _quit(session: _DriverSession) -> None:
    """A short WebDriver quit (geckodriver closes the Firefox it started),
    then the driver process; never raises. At most about
    ``_QUIT_TIMEOUT_SECONDS`` + 2 * ``_PROCESS_WAIT_SECONDS``."""
    session.config.timeout = _QUIT_TIMEOUT_SECONDS
    try:
        session.driver.quit()
    except Exception as exc:  # the browser or device may already be gone
        logger.debug("Quitting the browser session failed: %s", exc)
    _stop(session.service)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, WebDriverException):
        return (exc.msg or type(exc).__name__).lower()
    return (str(exc) or type(exc).__name__).lower()


def _is_read_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (ReadTimeoutError, TimeoutError)):
        return True
    return isinstance(exc, MaxRetryError) and isinstance(exc.reason, ReadTimeoutError)


def _is_refused(exc: BaseException) -> bool:
    if isinstance(exc, (NewConnectionError, ConnectionRefusedError)):
        return True
    return isinstance(exc, MaxRetryError) and isinstance(exc.reason, NewConnectionError)


def _is_session_lost(exc: BaseException, session: _DriverSession) -> bool:
    """A dead session or driver: never a read timeout (a slow page)."""
    if _is_read_timeout(exc):
        return False
    if isinstance(exc, (InvalidSessionIdException, NoSuchDriverException)):
        return True
    if _is_refused(exc) or not _driver_running(session.service):
        return True
    if isinstance(exc, WebDriverException):
        text = _error_text(exc)
        return any(marker in text for marker in _SESSION_LOST_MARKERS)
    return False


def _driver_running(service: Service) -> bool:
    process = getattr(service, "process", None)
    return isinstance(process, subprocess.Popen) and process.poll() is None


def _is_timeout(exc: BaseException) -> bool:
    return _is_read_timeout(exc) or "timed out" in _error_text(exc)


def _press_key(driver: AppiumDriver, key: str) -> None:
    code = _ANDROID_KEYCODES.get(key.strip().lower())
    if code is None:
        raise ExecutionError(_UNKNOWN_KEY)
    driver.press_keycode(code)


def _check_url(url: str) -> None:
    if web_url_is_file(url):
        raise WebBrowserUnsupportedError(_NO_FILE_URLS)


def _uses_selector(target: WebElementTarget | None) -> bool:
    return target is not None and (target.css is not None or target.xpath is not None)


class _VersionCache:
    def __init__(self, serial: str | None, package: str) -> None:
        self._serial = serial
        self._package = package
        self._version = ""
        self._checked = 0.0

    def get(self) -> str:
        if self._version:
            return self._version
        now = time.monotonic()
        if self._checked and now - self._checked < _VERSION_RECHECK_SECONDS:
            return ""
        self._checked = now
        self._version = android_app_version(self._serial, self._package)
        return self._version


# --- Chrome and Edge ---------------------------------------------------------


class AndroidChromiumBackend:
    """Chrome or Edge on the device, attached through chromedriver."""

    def __init__(
        self,
        browser: WebBrowser,
        package: str,
        context: WebBrowserContext,
        presence: AndroidPresence,
        resolver: _DriverResolver,
    ) -> None:
        self._browser = browser
        self._package = package
        self._serial = context.device_serial
        self._mobile: MobileDevice | None = context.mobile
        self._presence = presence
        self._resolver = resolver
        self._versions = _VersionCache(context.device_serial, package)
        # Edge may expose no DevTools socket: accessibility mode then.
        self._a11y_fallback = browser is WebBrowser.EDGE
        self._session: _DriverSession | None = None
        self._session_socket = ""
        self._listing = PageListing()
        self._a11y = a11y.A11yListing()
        self._mode: WebBrowserMode | None = None

    @property
    def browser(self) -> WebBrowser:
        return self._browser

    @property
    def how_to_open(self) -> WebBrowserHowToOpen:
        return WebBrowserHowToOpen.APP

    def is_installed(self) -> bool:
        return bool(self._versions.get())

    def version(self) -> str:
        return self._versions.get()

    def is_open(self) -> bool:
        return self._package in self._presence.snapshot().running

    def mode(self) -> WebBrowserMode | None:
        snapshot = self._presence.snapshot()
        if self._package not in snapshot.running:
            return None
        if self._socket(snapshot) is not None:
            return WebBrowserMode.PAGE
        if self._a11y_fallback and self._mobile is not None:
            return WebBrowserMode.ACCESSIBILITY
        return None

    def open(self, url: str | None) -> WebOpenResult:
        if url is not None:
            _check_url(url)
        deadline = time.monotonic() + _OPEN_BUDGET_SECONDS
        self._presence.invalidate()
        was_open = self.is_open()
        self._listing.reset()
        self._a11y.reset()
        if not was_open:
            android_start_app(self._serial, self._package, url)
            self._wait_started()
            self._connect(deadline, _OPEN_CREATE_TIMEOUT_SECONDS)
        elif url is not None:
            self._navigate(url, deadline, _OPEN_CREATE_TIMEOUT_SECONDS)
        else:
            self._connect(deadline, _OPEN_CREATE_TIMEOUT_SECONDS)
        return WebOpenResult(
            mode=self._mode or WebBrowserMode.PAGE,
            version=self.version(),
            attached=was_open,
        )

    def navigate(self, url: str) -> str:
        _check_url(url)
        return self._navigate(
            url,
            time.monotonic() + _ACTION_BUDGET_SECONDS,
            _ATTACH_CREATE_TIMEOUT_SECONDS,
        )

    def history(self, action: WebHistoryAction) -> str:
        return self._run(
            lambda driver: page_history(driver, self._listing, action),
            self._page_only,
        )

    def tabs(self, action: WebTabsAction, index: int | None) -> str:
        return self._run(
            lambda driver: page_tabs(driver, self._listing, action, index),
            self._page_only,
        )

    def elements(self, scope_css: str | None, max_elements: int) -> str:
        def accessible(driver: AppiumDriver) -> str:
            if scope_css is not None:
                raise WebBrowserUnsupportedError(_PAGE_ONLY)
            page = self._a11y_page(driver)
            return a11y.list_elements(page, self._a11y, max_elements)

        return self._run(
            lambda driver: page_elements(
                driver, self._listing, scope_css, max_elements
            ),
            accessible,
        )

    def act(self, request: WebActRequest) -> str:
        if request.action is WebActKind.HOVER:
            raise WebBrowserUnsupportedError(_NO_HOVER)

        def accessible(driver: AppiumDriver) -> str:
            if _uses_selector(request.target):
                raise WebBrowserUnsupportedError(_PAGE_ONLY)
            return a11y.act(driver, self._a11y, request, _press_key)

        return self._run(
            lambda driver: page_act(driver, self._listing, request, allow_hover=False),
            accessible,
        )

    def read(self, request: WebReadRequest) -> str:
        def accessible(driver: AppiumDriver) -> str:
            if _uses_selector(request.target) or (
                request.what is WebReadWhat.ATTRIBUTE
            ):
                raise WebBrowserUnsupportedError(_PAGE_ONLY)
            return a11y.read(self._a11y_page(driver), self._a11y, request)

        return self._run(
            lambda driver: page_read(driver, self._listing, request), accessible
        )

    def wait(
        self, target: WebElementTarget, state: WebWaitState, timeout_ms: int
    ) -> str:
        return self._run(
            lambda driver: page_wait(driver, self._listing, target, state, timeout_ms),
            self._page_only,
        )

    def evaluate(self, script: str, args_json: str, timeout_ms: int) -> WebEvalResult:
        return self._run(
            lambda driver: evaluate_script(driver, script, args_json, timeout_ms),
            self._page_only_eval,
        )

    def close(self) -> str:
        self._detach()
        return _DETACHED

    def shutdown(self) -> None:
        self._detach()

    def cleanup(self) -> None:
        # Phone apps are left to the device cleanup: detach only.
        self._detach()

    # --- helpers ----------------------------------------------------------

    def _socket(self, snapshot: AndroidSnapshot) -> str | None:
        """This app's DevTools socket. Chrome: Chrome's name. Edge
        (Unverified): its own "<package>_devtools_remote", else Chrome's
        name while Chrome itself is not running (both cannot own it)."""
        own = f"{self._package}{_OWN_SOCKET_SUFFIX}"
        if self._browser is WebBrowser.CHROME:
            return _CHROME_SOCKET if _CHROME_SOCKET in snapshot.sockets else None
        if own in snapshot.sockets:
            return own
        if _CHROME_SOCKET in snapshot.sockets and CHROME_PACKAGE not in (
            snapshot.running
        ):
            return _CHROME_SOCKET
        return None

    def _wait_started(self) -> None:
        """Until the app runs with its socket, or the waits run out (Edge
        then continues in accessibility mode)."""
        deadline = time.monotonic() + _START_WAIT_SECONDS
        socket_deadline: float | None = None
        while True:
            self._presence.invalidate()
            snapshot = self._presence.snapshot()
            now = time.monotonic()
            if self._package in snapshot.running:
                if self._socket(snapshot) is not None:
                    return
                if socket_deadline is None:
                    socket_deadline = now + _SOCKET_AFTER_START_SECONDS
            if now >= deadline or (
                socket_deadline is not None and now >= socket_deadline
            ):
                return
            time.sleep(_POLL_SECONDS)

    def _navigate(self, url: str, deadline: float, create_timeout: int) -> str:
        def accessible(driver: AppiumDriver) -> str:
            android_start_app(self._serial, self._package, url)
            self._a11y.reset()
            return url

        return self._run(
            lambda driver: page_navigate(driver, self._listing, url),
            accessible,
            deadline,
            create_timeout,
        )

    def _connect(self, deadline: float, create_timeout: int) -> None:
        """Attach (or check accessibility mode) without running a command."""
        session = self._page_session(deadline, create_timeout)
        if session is None:
            self._a11y_call(lambda driver: "", deadline)

    def _page_session(
        self, deadline: float, create_timeout: int
    ) -> _DriverSession | None:
        """The attached page session; None in accessibility mode."""
        snapshot = self._presence.snapshot()
        if self._package not in snapshot.running:
            self._detach()
            raise WebBrowserNotOpenError(self._browser.value)
        socket = self._socket(snapshot)
        if socket is None:
            self._detach()
            if self._a11y_fallback and self._mobile is not None:
                self._mode = WebBrowserMode.ACCESSIBILITY
                return None
            raise WebBrowserSetupNeededError(
                WebBrowserSetupStep.ANDROID_DEVTOOLS_UNAVAILABLE
            )
        self._mode = WebBrowserMode.PAGE
        return self._attach(socket, create_timeout, deadline)

    def _run[T](
        self,
        page_op: Callable[[WebDriver], T],
        a11y_op: Callable[[AppiumDriver], T],
        deadline: float | None = None,
        create_timeout: int = _ATTACH_CREATE_TIMEOUT_SECONDS,
    ) -> T:
        if deadline is None:
            deadline = time.monotonic() + _ACTION_BUDGET_SECONDS
        session = self._page_session(deadline, create_timeout)
        if session is None:
            return self._a11y_call(a11y_op, deadline)
        _require_command_time(deadline)
        try:
            return page_op(session.driver)
        except ExecutionError:
            raise
        except _CALL_ERRORS as exc:
            raise self._command_error(exc, session) from exc

    def _command_error(
        self, exc: BaseException, session: _DriverSession
    ) -> ExecutionError:
        """A fixed text for a failed command; a lost session is dropped (the
        next call attaches again). Never retried here."""
        logger.warning("%s command failed: %s", self._browser, _error_text(exc)[:300])
        if _is_read_timeout(exc):
            return ExecutionError(_OUT_OF_TIME)
        if not _is_session_lost(exc, session):
            return ExecutionError(_FAILED)
        self._detach()
        self._presence.invalidate()
        if self._package not in self._presence.snapshot().running:
            return WebBrowserNotOpenError(self._browser.value)
        return ExecutionError(_LOST)

    def _a11y_call[T](
        self, operation: Callable[[AppiumDriver], T], deadline: float
    ) -> T:
        mobile = self._mobile
        if mobile is None:
            raise WebBrowserSetupNeededError(
                WebBrowserSetupStep.ANDROID_DEVTOOLS_UNAVAILABLE
            )
        # The phone session answers within its own budget, which must fit
        # in what is left of this call's.
        if deadline - time.monotonic() < _PHONE_SESSION_BUDGET_SECONDS:
            raise ExecutionError(_PHONE_SLOW)

        def in_front(driver: AppiumDriver) -> T:
            try:
                if driver.current_package != self._package:
                    driver.activate_app(self._package)
                return operation(driver)
            except (InvalidSessionIdException, NoSuchDriverException):
                raise  # the phone session recreates it
            except WebDriverException as exc:
                # The phone session would pass the raw text on.
                logger.warning("Browser action on the phone failed: %s", exc.msg)
                raise ExecutionError(_FAILED) from exc

        return mobile.web_call(in_front)

    def _a11y_page(self, driver: AppiumDriver) -> a11y.A11yPage:
        return a11y.parse_android_page(driver.page_source, self._package)

    def _page_only(self, driver: AppiumDriver) -> str:
        raise WebBrowserUnsupportedError(_PAGE_ONLY)

    def _page_only_eval(self, driver: AppiumDriver) -> WebEvalResult:
        raise WebBrowserUnsupportedError(_PAGE_ONLY)

    def _attach(
        self, socket: str, create_timeout: int, deadline: float
    ) -> _DriverSession:
        session = self._session
        if session is not None and self._session_socket == socket:
            if _driver_running(session.service):
                return session
        self._detach()
        path = self._resolver.resolve("chrome", "chromedriver", _major(self.version()))
        options = ChromeOptions()
        options.enable_mobile(android_package=self._package, device_serial=self._serial)
        options.add_experimental_option("androidUseRunningApp", True)
        options.add_experimental_option("androidDeviceSocket", socket)
        options.page_load_strategy = "eager"
        service = ChromeService(executable_path=path, log_output=subprocess.DEVNULL)
        before = adb_forwards(self._serial)
        timeout = _create_timeout(create_timeout, deadline)
        try:
            session = _start_session(
                service,
                lambda config: ChromiumRemoteConnection(
                    remote_server_addr=config.remote_server_addr,
                    vendor_prefix="goog",
                    browser_name="chrome",
                    client_config=config,
                ),
                options,
                timeout,
            )
        except _CALL_ERRORS as exc:
            _remove_forwards(self._serial, _new_forwards(before, self._serial, socket))
            raise self._attach_error(exc) from exc
        session.forwards = _new_forwards(before, self._serial, socket)
        self._session, self._session_socket = session, socket
        self._listing.reset()
        return session

    def _attach_error(self, exc: BaseException) -> ExecutionError:
        text = _error_text(exc)
        logger.warning("Attaching to %s failed: %s", self._browser, text[:300])
        if any(marker in text for marker in _DRIVER_MISMATCH_MARKERS):
            return WebBrowserSetupNeededError(WebBrowserSetupStep.DRIVER_UNAVAILABLE)
        if _is_timeout(exc):
            return ExecutionError(_SLOW_START)
        if any(marker in text for marker in _DEVTOOLS_MARKERS):
            return WebBrowserSetupNeededError(
                WebBrowserSetupStep.ANDROID_DEVTOOLS_UNAVAILABLE
            )
        return ExecutionError(_NOT_STARTED)

    def _detach(self) -> None:
        session, self._session = self._session, None
        self._session_socket = ""
        self._listing.reset()
        if session is not None:
            _kill(session.service)
            # chromedriver removes its forwards only on quit, which is skipped.
            _remove_forwards(self._serial, session.forwards)


# --- Firefox -----------------------------------------------------------------


class AndroidFirefoxBackend:
    """Firefox on the device through geckodriver (its own test profile)."""

    def __init__(
        self,
        context: WebBrowserContext,
        presence: AndroidPresence,
        resolver: _DriverResolver,
    ) -> None:
        self._serial = context.device_serial
        self._presence = presence
        self._resolver = resolver
        self._versions = _VersionCache(context.device_serial, FIREFOX_PACKAGE)
        self._session: _DriverSession | None = None
        self._listing = PageListing()

    @property
    def browser(self) -> WebBrowser:
        return WebBrowser.FIREFOX

    @property
    def how_to_open(self) -> WebBrowserHowToOpen:
        return WebBrowserHowToOpen.OPEN_TOOL_ONLY

    def is_installed(self) -> bool:
        return bool(self._versions.get())

    def version(self) -> str:
        return self._versions.get()

    def is_open(self) -> bool:
        if self._session is None:
            return False
        if FIREFOX_PACKAGE in self._presence.snapshot().running:
            return True
        # Closed on the phone: the session is gone with it.
        self._end()
        return False

    def mode(self) -> WebBrowserMode | None:
        return WebBrowserMode.PAGE if self.is_open() else None

    def open(self, url: str | None) -> WebOpenResult:
        if url is not None:
            _check_url(url)
        deadline = time.monotonic() + _OPEN_BUDGET_SECONDS
        self._presence.invalidate()
        attached = self.is_open()
        if not attached:
            self._session = self._create(deadline)
            self._listing.reset()
        if url is not None:
            self._page(
                lambda driver: page_navigate(driver, self._listing, url), deadline
            )
        return WebOpenResult(
            mode=WebBrowserMode.PAGE, version=self.version(), attached=attached
        )

    def navigate(self, url: str) -> str:
        _check_url(url)
        return self._page(lambda driver: page_navigate(driver, self._listing, url))

    def history(self, action: WebHistoryAction) -> str:
        return self._page(lambda driver: page_history(driver, self._listing, action))

    def tabs(self, action: WebTabsAction, index: int | None) -> str:
        return self._page(
            lambda driver: page_tabs(driver, self._listing, action, index)
        )

    def elements(self, scope_css: str | None, max_elements: int) -> str:
        return self._page(
            lambda driver: page_elements(driver, self._listing, scope_css, max_elements)
        )

    def act(self, request: WebActRequest) -> str:
        if request.action is WebActKind.HOVER:
            raise WebBrowserUnsupportedError(_NO_HOVER)
        return self._page(
            lambda driver: page_act(driver, self._listing, request, allow_hover=False)
        )

    def read(self, request: WebReadRequest) -> str:
        return self._page(lambda driver: page_read(driver, self._listing, request))

    def wait(
        self, target: WebElementTarget, state: WebWaitState, timeout_ms: int
    ) -> str:
        return self._page(
            lambda driver: page_wait(driver, self._listing, target, state, timeout_ms)
        )

    def evaluate(self, script: str, args_json: str, timeout_ms: int) -> WebEvalResult:
        return self._page(
            lambda driver: evaluate_script(driver, script, args_json, timeout_ms)
        )

    def close(self) -> str:
        self._end()
        return _CLOSED

    def shutdown(self) -> None:
        self._end()

    def cleanup(self) -> None:
        self._end()

    # --- helpers ----------------------------------------------------------

    def _page[T](
        self, operation: Callable[[WebDriver], T], deadline: float | None = None
    ) -> T:
        if deadline is None:
            deadline = time.monotonic() + _ACTION_BUDGET_SECONDS
        session = self._session
        if not self.is_open() or session is None:
            raise WebBrowserNotOpenError(WebBrowser.FIREFOX.value)
        _require_command_time(deadline)
        try:
            return operation(session.driver)
        except ExecutionError:
            raise
        except _CALL_ERRORS as exc:
            logger.warning("Firefox command failed: %s", _error_text(exc)[:300])
            if _is_read_timeout(exc):
                raise ExecutionError(_OUT_OF_TIME) from exc
            if not _is_session_lost(exc, session):
                raise ExecutionError(_FAILED) from exc
            self._end()
            raise WebBrowserNotOpenError(WebBrowser.FIREFOX.value) from exc

    def _create(self, deadline: float) -> _DriverSession:
        path = self._resolver.resolve("firefox", "geckodriver", "")
        options = FirefoxOptions()
        options.enable_mobile(
            android_package=FIREFOX_PACKAGE, device_serial=self._serial
        )
        options.page_load_strategy = "eager"
        service = FirefoxService(executable_path=path, log_output=subprocess.DEVNULL)
        try:
            return _start_session(
                service,
                lambda config: FirefoxRemoteConnection(
                    remote_server_addr=config.remote_server_addr,
                    client_config=config,
                ),
                options,
                _create_timeout(_FIREFOX_CREATE_TIMEOUT_SECONDS, deadline),
            )
        except _CALL_ERRORS as exc:
            raise self._create_error(exc) from exc

    def _create_error(self, exc: BaseException) -> ExecutionError:
        text = _error_text(exc)
        logger.warning("Starting Firefox on the phone failed: %s", text[:300])
        if isinstance(exc, (Urllib3HTTPError, TimeoutError)):
            return ExecutionError(_SLOW_START)
        if any(marker in text for marker in _FIREFOX_REMOTE_OFF_MARKERS):
            return WebBrowserSetupNeededError(
                WebBrowserSetupStep.FIREFOX_ANDROID_REMOTE_DEBUGGING_OFF
            )
        return ExecutionError(_NOT_STARTED)

    def _end(self) -> None:
        session, self._session = self._session, None
        self._listing.reset()
        if session is not None:
            _quit(session)
