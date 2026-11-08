"""Desktop backends of the web browser layer (WEB_BROWSER_V1).

- Chrome / Edge: started by ``web_browser_launch`` with the dedicated profile;
  chromedriver / msedgedriver attaches through ``debuggerAddress`` to the
  port in the profile's ``DevToolsActivePort``.
- Firefox: started the same way; geckodriver attaches with
  ``--connect-existing --marionette-port`` to the port in
  ``MarionetteActivePort``.
- Safari: ``safaridriver`` always opens its own automation window and cannot
  attach to a person's window, so the session is the browser
  (``how_to_open`` = open tool only).

Selenium Manager resolves chromedriver, msedgedriver and geckodriver (and
downloads them unless ``context.driver_download`` is off, then it runs
offline: PATH or its cache only). Every session uses the ``eager`` page-load
strategy (Safari: the default) and is dropped when its browser goes away.

Timeouts: every session is built here (not through Selenium's ``Chrome`` /
``Firefox`` / ``Safari`` classes, which fix a 120 s HTTP timeout) with a
``ClientConfig`` HTTP timeout, and gets page-load and script timeouts, all
below the hub's answer budget, so a hung page (``while(true){}``, a stuck
dialog) fails the request instead of blocking its thread for minutes. A
request that hit the HTTP timeout drops the session (the driver may still be
busy with it); the next call attaches afresh. Safari is the exception: its
session is the browser, so a timeout keeps it (``_DROP_ON_READ_TIMEOUT``).

Ending a session (``shutdown``) stops the driver process only (terminate,
then kill; never Selenium's slow graceful stop), so a person's browser stays
open; ``close`` quits the browser. ``cleanup`` (environment cleanup) never
attaches or starts a driver: it ends the dedicated profile's processes by
profile path with short terminate-then-kill waits.
"""

import logging
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, TypeVar

import psutil
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchDriverException,
    NoSuchWindowException,
    SessionNotCreatedException,
    WebDriverException,
)
from selenium.webdriver import (
    ChromeOptions,
    ChromeService,
    EdgeOptions,
    EdgeService,
    FirefoxOptions,
    FirefoxService,
    SafariOptions,
    SafariService,
)
from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection
from selenium.webdriver.common.options import BaseOptions
from selenium.webdriver.common.selenium_manager import SeleniumManager
from selenium.webdriver.common.service import Service
from selenium.webdriver.firefox.remote_connection import FirefoxRemoteConnection
from selenium.webdriver.remote.client_config import ClientConfig
from selenium.webdriver.remote.remote_connection import RemoteConnection
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.safari.remote_connection import SafariRemoteConnection
from urllib3.exceptions import HTTPError, ReadTimeoutError

from controller_client.exceptions import (
    BrowserNotInstalledError,
    ExecutionError,
    WebBrowserNotOpenError,
    WebBrowserSetupNeededError,
)
from controller_client.protocol import (
    WebBrowser,
    WebBrowserHowToOpen,
    WebBrowserMode,
    WebBrowserSetupStep,
    WebElementTarget,
    WebHistoryAction,
    WebTabsAction,
    WebWaitState,
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
from controller_client.web_browser_launch import (
    find_executable,
    find_profile_processes,
    installed_version,
    launch_browser,
    profile_dir,
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
from controller_client.web_browser_presence import (
    DEVTOOLS_PORT_FILE,
    MARIONETTE_PORT_FILE,
    chromium_open_port,
    firefox_open_port,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Below WEB_BROWSER_ACTION_BUDGET_SECONDS (24): the page-load and script
# timeouts answer inside the HTTP timeout, which answers inside the budget.
_HTTP_TIMEOUT_SECONDS: Final[int] = 20
_PAGE_LOAD_TIMEOUT_SECONDS: Final[float] = 15.0
# Safari's quit on close, cleanup and shutdown: never the full HTTP timeout.
_SAFARI_QUIT_TIMEOUT_SECONDS: Final[int] = 3
# Selenium Manager's network timeout, so a stalled download ends.
_SELENIUM_MANAGER_TIMEOUT: Final[str] = "30"
# After a launch: how long the browser may take to write its port file.
_START_WAIT_SECONDS: Final[float] = 40.0
_START_POLL_SECONDS: Final[float] = 0.25
# After a graceful close: how long before the profile's processes are ended.
_CLOSE_WAIT_SECONDS: Final[float] = 5.0
# Terminate, wait, kill, wait: environment cleanup of up to three browsers
# stays well under 10 s.
_TERMINATE_WAIT_SECONDS: Final[float] = 1.5
_KILL_WAIT_SECONDS: Final[float] = 0.5
_DRIVER_STOP_WAIT_SECONDS: Final[float] = 1.0
_CHROMIUM_CONNECTIONS: Final[dict[WebBrowser, tuple[str, str]]] = {
    # browser -> (WebDriver browserName, vendor prefix of its commands)
    WebBrowser.CHROME: ("chrome", "goog"),
    WebBrowser.EDGE: ("MicrosoftEdge", "ms"),
}
_SAFARI_APP: Final[Path] = Path("/Applications/Safari.app")
_SAFARIDRIVER: Final[Path] = Path("/usr/bin/safaridriver")
# Selenium Manager's ``--browser`` names.
_MANAGER_NAMES: Final[dict[WebBrowser, str]] = {
    WebBrowser.CHROME: "chrome",
    WebBrowser.EDGE: "MicrosoftEdge",
    WebBrowser.FIREFOX: "firefox",
}
# Session errors that mean the driver does not fit the installed browser.
_DRIVER_MISMATCH_MARKERS: Final[tuple[str, ...]] = (
    "only supports",
    "unable to obtain driver",
)
_SAFARI_SETUP_MARKERS: Final[tuple[str, ...]] = (
    "remote automation",
    "safaridriver --enable",
)
_SESSION_ERRORS: Final[tuple[type[Exception], ...]] = (
    WebDriverException,
    HTTPError,
    OSError,
)


def _resolve_driver(browser: WebBrowser, executable: Path, download: bool) -> str:
    """The driver path from Selenium Manager (offline when downloads are
    off); WebBrowserSetupNeededError(DRIVER_UNAVAILABLE) when it has none.

    ``--offline`` is Selenium Manager's flag for SE_OFFLINE=true; passed per
    call so the process environment stays untouched."""
    args = [
        "--browser",
        _MANAGER_NAMES[browser],
        "--browser-path",
        str(executable),
        "--timeout",
        _SELENIUM_MANAGER_TIMEOUT,
    ]
    if not download:
        args.append("--offline")
    try:
        result = SeleniumManager().binary_paths(args)
    except WebDriverException as e:
        logger.debug(
            "Selenium Manager found no %s driver", browser.value, exc_info=True
        )
        raise WebBrowserSetupNeededError(WebBrowserSetupStep.DRIVER_UNAVAILABLE) from e
    path = result.get("driver_path") if isinstance(result, dict) else None
    if not isinstance(path, str) or not Path(path).is_file():
        raise WebBrowserSetupNeededError(WebBrowserSetupStep.DRIVER_UNAVAILABLE)
    return path


def _client_config(service_url: str) -> ClientConfig:
    """HTTP timeout below the budget and no urllib3 retries (a retried GET
    would wait the timeout again)."""
    return ClientConfig(
        remote_server_addr=service_url,
        timeout=_HTTP_TIMEOUT_SECONDS,
        # Selenium reads the pool manager arguments from this nested key.
        init_args_for_pool_manager={"init_args_for_pool_manager": {"retries": False}},
    )


def _stop_service(service: Service | None) -> None:
    """Stop a driver process quickly: terminate, then kill (Selenium's own
    ``stop`` may wait over a minute on a busy driver)."""
    process = service.process if service is not None else None
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(_DRIVER_STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(_DRIVER_STOP_WAIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        logger.debug("Could not stop the browser driver", exc_info=True)


def _start_session(
    service: Service,
    options: BaseOptions,
    connection: Callable[[str], RemoteConnection],
) -> WebDriver:
    """Start ``service`` and open a session on it through ``connection``
    (built with ``_client_config``); the service is stopped on failure."""
    service.start()
    try:
        return WebDriver(
            command_executor=connection(service.service_url), options=options
        )
    except BaseException:
        _stop_service(service)
        raise


def _end_processes(profile: Path, grace_seconds: float) -> None:
    """Wait ``grace_seconds`` for the profile's browser to exit, then
    terminate and finally kill what is left."""
    processes = find_profile_processes(profile)
    if not processes:
        return
    alive = processes
    if grace_seconds > 0:
        _gone, alive = psutil.wait_procs(alive, timeout=grace_seconds)
    for process in alive:
        try:
            process.terminate()
        except psutil.Error:
            continue
    _gone, alive = psutil.wait_procs(alive, timeout=_TERMINATE_WAIT_SECONDS)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            continue
    psutil.wait_procs(alive, timeout=_KILL_WAIT_SECONDS)


class _PageBackend:
    """What every desktop backend shares: one session, one listing, the page
    operations and the error rule (browser gone -> not open)."""

    # Whether a read timeout drops the session. True where the session only
    # attaches to a browser that outlives it; False where the session is the
    # browser (Safari), so a slow page never closes it.
    _DROP_ON_READ_TIMEOUT: bool = True

    def __init__(self, browser: WebBrowser, context: WebBrowserContext) -> None:
        self._browser = browser
        self._context = context
        self._driver: WebDriver | None = None
        self._service: Service | None = None
        self._listing = PageListing()
        self._session_version = ""

    @property
    def browser(self) -> WebBrowser:
        return self._browser

    def is_open(self) -> bool:
        raise NotImplementedError

    def _session(self) -> WebDriver:
        """The live session, attaching when needed; WebBrowserNotOpenError
        when the browser is not open."""
        raise NotImplementedError

    def mode(self) -> WebBrowserMode | None:
        return WebBrowserMode.PAGE if self.is_open() else None

    def _drop(self) -> None:
        self._listing.reset()
        self._driver = None
        service, self._service = self._service, None
        _stop_service(service)

    def _adopt(self, driver: WebDriver, service: Service | None) -> WebDriver:
        try:
            driver.set_page_load_timeout(_PAGE_LOAD_TIMEOUT_SECONDS)
            driver.set_script_timeout(WEB_SCRIPT_TIMEOUT_SECONDS)
        except _SESSION_ERRORS:
            logger.debug("Could not set the session timeouts", exc_info=True)
        version = driver.capabilities.get("browserVersion")
        if isinstance(version, str):
            self._session_version = version
        self._driver = driver
        self._service = service
        self._listing.reset()
        return driver

    def _run(self, operation: Callable[[WebDriver], _T]) -> _T:
        driver = self._session()
        try:
            return operation(driver)
        except ExecutionError:
            raise
        except InvalidSessionIdException as e:
            self._drop()
            raise ExecutionError("Lost the browser session; try again") from e
        except ReadTimeoutError as e:
            # The driver may still be stuck on the request: start afresh,
            # unless the session is the browser itself.
            if self._DROP_ON_READ_TIMEOUT:
                self._drop()
            else:
                self._listing.reset()
            raise ExecutionError("The browser did not answer in time; try again") from e
        except NoSuchWindowException as e:
            self._listing.reset()
            try:
                handles = driver.window_handles
            except ReadTimeoutError as timeout:
                if not self._DROP_ON_READ_TIMEOUT:
                    raise ExecutionError(
                        "The browser did not answer in time; try again"
                    ) from timeout
                handles = []
            except _SESSION_ERRORS:
                handles = []
            if not handles:
                self._drop()
                raise WebBrowserNotOpenError(self._browser.value) from e
            driver.switch_to.window(handles[-1])
            raise ExecutionError("The tab was closed; try again") from e
        except _SESSION_ERRORS as e:
            logger.debug("Browser action failed", exc_info=True)
            if not self.is_open():
                self._drop()
                raise WebBrowserNotOpenError(self._browser.value) from e
            raise ExecutionError("Browser action failed") from e

    def navigate(self, url: str) -> str:
        return self._run(lambda driver: page_navigate(driver, self._listing, url))

    def history(self, action: WebHistoryAction) -> str:
        return self._run(lambda driver: page_history(driver, self._listing, action))

    def tabs(self, action: WebTabsAction, index: int | None) -> str:
        return self._run(lambda driver: page_tabs(driver, self._listing, action, index))

    def elements(self, scope_css: str | None, max_elements: int) -> str:
        return self._run(
            lambda driver: page_elements(driver, self._listing, scope_css, max_elements)
        )

    def act(self, request: WebActRequest) -> str:
        return self._run(
            lambda driver: page_act(driver, self._listing, request, allow_hover=True)
        )

    def read(self, request: WebReadRequest) -> str:
        return self._run(lambda driver: page_read(driver, self._listing, request))

    def wait(
        self, target: WebElementTarget, state: WebWaitState, timeout_ms: int
    ) -> str:
        return self._run(
            lambda driver: page_wait(driver, self._listing, target, state, timeout_ms)
        )

    def evaluate(self, script: str, args_json: str, timeout_ms: int) -> WebEvalResult:
        return self._run(
            lambda driver: evaluate_script(driver, script, args_json, timeout_ms)
        )


class _LauncherBackend(_PageBackend):
    """Chrome, Edge or Firefox started by the launcher with the dedicated
    profile; attached through the port in the profile."""

    def __init__(self, browser: WebBrowser, context: WebBrowserContext) -> None:
        super().__init__(browser, context)
        self._profile = profile_dir(context.runner_root, browser)
        self._executable: Path | None = None
        self._looked_up = False
        self._installed_version: str | None = None
        self._port: int | None = None
        self._driver_path: str | None = None

    @property
    def how_to_open(self) -> WebBrowserHowToOpen:
        return WebBrowserHowToOpen.SHORTCUT

    def _find_executable(self) -> Path | None:
        if not self._looked_up:
            self._executable = find_executable(self._browser)
            self._looked_up = True
        return self._executable

    def is_installed(self) -> bool:
        return self._find_executable() is not None

    def version(self) -> str:
        if self._session_version:
            return self._session_version
        executable = self._find_executable()
        if executable is None:
            return ""
        if self._installed_version is None:
            self._installed_version = installed_version(self._browser, executable)
        return self._installed_version

    def _open_port(self) -> int | None:
        raise NotImplementedError

    def _attach(self, port: int, driver_path: str) -> WebDriver:
        raise NotImplementedError

    def _port_file(self) -> Path:
        raise NotImplementedError

    def _close_windows(self, driver: WebDriver) -> None:
        raise NotImplementedError

    def is_open(self) -> bool:
        port = self._open_port()
        if port is None and self._driver is not None:
            self._drop()
        return port is not None

    def _driver_for(self, executable: Path) -> str:
        if self._driver_path is None or not Path(self._driver_path).is_file():
            self._driver_path = _resolve_driver(
                self._browser, executable, self._context.driver_download
            )
        return self._driver_path

    def _session(self) -> WebDriver:
        port = self._open_port()
        if port is None:
            self._drop()
            raise WebBrowserNotOpenError(self._browser.value)
        if self._driver is not None and self._port == port:
            return self._driver
        self._drop()
        executable = self._find_executable()
        if executable is None:
            raise BrowserNotInstalledError("Browser not installed")
        driver_path = self._driver_for(executable)
        try:
            driver = self._attach(port, driver_path)
        except NoSuchDriverException as e:
            raise WebBrowserSetupNeededError(
                WebBrowserSetupStep.DRIVER_UNAVAILABLE
            ) from e
        except SessionNotCreatedException as e:
            message = (e.msg or "").lower()
            if any(marker in message for marker in _DRIVER_MISMATCH_MARKERS):
                # A cached driver for another browser version: resolve again
                # next time.
                self._driver_path = None
                raise WebBrowserSetupNeededError(
                    WebBrowserSetupStep.DRIVER_UNAVAILABLE
                ) from e
            raise ExecutionError("Could not attach to the browser") from e
        except _SESSION_ERRORS as e:
            logger.debug("Attaching to %s failed", self._browser.value, exc_info=True)
            raise ExecutionError("Could not attach to the browser") from e
        self._port = port
        return driver

    def open(self, url: str | None) -> WebOpenResult:
        if self._open_port() is not None:
            self._session()
            if url is not None:
                self.navigate(url)
            return WebOpenResult(
                mode=WebBrowserMode.PAGE, version=self.version(), attached=True
            )
        launch_browser(self._browser, self._context.runner_root, url)
        deadline = time.monotonic() + _START_WAIT_SECONDS
        while self._open_port() is None:
            if time.monotonic() >= deadline:
                raise ExecutionError("The browser did not start in time; try again")
            time.sleep(_START_POLL_SECONDS)
        self._session()
        return WebOpenResult(
            mode=WebBrowserMode.PAGE, version=self.version(), attached=False
        )

    def close(self) -> str:
        driver = self._driver
        if driver is None and self._open_port() is not None:
            try:
                driver = self._session()
            except ExecutionError:
                driver = None
        if driver is not None:
            try:
                self._close_windows(driver)
            except _SESSION_ERRORS:
                logger.debug("Closing the browser windows failed", exc_info=True)
        self._forget_session()
        _end_processes(self._profile, _CLOSE_WAIT_SECONDS)
        return "Closed the browser"

    def _forget_session(self) -> None:
        self._drop()
        self._port = None
        self._session_version = ""

    def shutdown(self) -> None:
        self._drop()
        self._port = None

    def cleanup(self) -> None:
        """No attach, no driver: stop a running session's driver, then end
        the dedicated profile's processes. The process table is scanned only
        while the profile's port file exists (a running browser keeps it),
        so a cleanup with nothing open costs one stat."""
        try:
            self._forget_session()
            if self._port_file().exists():
                _end_processes(self._profile, 0.0)
        except Exception:  # noqa: BLE001 - cleanup must never raise
            logger.debug("Browser cleanup failed", exc_info=True)


class ChromiumDesktopBackend(_LauncherBackend):
    """Desktop Chrome or Edge."""

    def _open_port(self) -> int | None:
        return chromium_open_port(self._profile)

    def _port_file(self) -> Path:
        return self._profile / DEVTOOLS_PORT_FILE

    def _attach(self, port: int, driver_path: str) -> WebDriver:
        options = EdgeOptions() if self._browser is WebBrowser.EDGE else ChromeOptions()
        options.debugger_address = f"127.0.0.1:{port}"
        options.page_load_strategy = "eager"
        service: Service = (
            EdgeService(executable_path=driver_path)
            if self._browser is WebBrowser.EDGE
            else ChromeService(executable_path=driver_path)
        )
        browser_name, vendor_prefix = _CHROMIUM_CONNECTIONS[self._browser]
        driver = _start_session(
            service,
            options,
            lambda url: ChromiumRemoteConnection(
                remote_server_addr=url,
                vendor_prefix=vendor_prefix,
                browser_name=browser_name,
                client_config=_client_config(url),
            ),
        )
        return self._adopt(driver, service)

    def _close_windows(self, driver: WebDriver) -> None:
        # ChromiumDriver.execute_cdp_cmd, on the plain WebDriver this backend
        # builds (its ChromiumRemoteConnection knows the command).
        driver.execute("executeCdpCommand", {"cmd": "Browser.close", "params": {}})


class FirefoxDesktopBackend(_LauncherBackend):
    """Desktop Firefox."""

    def _open_port(self) -> int | None:
        return firefox_open_port(self._profile)

    def _port_file(self) -> Path:
        return self._profile / MARIONETTE_PORT_FILE

    def _attach(self, port: int, driver_path: str) -> WebDriver:
        options = FirefoxOptions()
        # geckodriver refuses Firefox preferences when it attaches to a
        # running Firefox; Selenium sets one by default.
        options.preferences.clear()
        options.page_load_strategy = "eager"
        service = FirefoxService(
            executable_path=driver_path,
            service_args=["--connect-existing", "--marionette-port", str(port)],
        )
        driver = _start_session(
            service,
            options,
            lambda url: FirefoxRemoteConnection(
                remote_server_addr=url, client_config=_client_config(url)
            ),
        )
        return self._adopt(driver, service)

    def _close_windows(self, driver: WebDriver) -> None:
        for handle in list(driver.window_handles):
            driver.switch_to.window(handle)
            driver.close()


class SafariDesktopBackend(_PageBackend):
    """macOS Safari: the runner's safaridriver session is the browser."""

    _DROP_ON_READ_TIMEOUT = False

    def __init__(self, context: WebBrowserContext) -> None:
        super().__init__(WebBrowser.SAFARI, context)
        self._installed_version: str | None = None
        # Set after one probe went unanswered; cleared when one answers.
        self._unanswered = False

    def _drop(self) -> None:
        # Every way a session ends (quit, lost session, last window closed)
        # resets the per-session state, so a new session starts clean.
        super()._drop()
        self._unanswered = False
        self._session_version = ""

    @property
    def how_to_open(self) -> WebBrowserHowToOpen:
        return WebBrowserHowToOpen.OPEN_TOOL_ONLY

    def is_installed(self) -> bool:
        return (
            sys.platform == "darwin"
            and _SAFARI_APP.is_dir()
            and _SAFARIDRIVER.is_file()
        )

    def version(self) -> str:
        if self._session_version:
            return self._session_version
        if self._installed_version is None:
            executable = _SAFARI_APP / "Contents" / "MacOS" / "Safari"
            self._installed_version = (
                installed_version(WebBrowser.SAFARI, executable)
                if self.is_installed()
                else ""
            )
        return self._installed_version

    def is_open(self) -> bool:
        driver = self._driver
        if driver is None:
            return False
        try:
            if driver.window_handles:
                self._unanswered = False
                return True
        except ReadTimeoutError:
            # The session is the browser, so one unanswered probe keeps it
            # (still busy with a slow page). A live safaridriver answers
            # within the HTTP timeout (page-load and script timeouts are
            # below it), so a second one in a row means it is wedged: end it.
            if not self._unanswered:
                self._unanswered = True
                return True
            logger.warning("safaridriver stopped answering; ending the session")
        except _SESSION_ERRORS:
            logger.debug("The Safari session is gone", exc_info=True)
        self._quit()
        return False

    def _session(self) -> WebDriver:
        if not self.is_open() or self._driver is None:
            raise WebBrowserNotOpenError(self._browser.value)
        return self._driver

    def _quit(self) -> None:
        """End the session with a short HTTP timeout, then stop the driver
        process (terminate, then kill)."""
        driver = self._driver
        if driver is not None:
            executor = driver.command_executor
            if isinstance(executor, RemoteConnection):
                executor.client_config.timeout = _SAFARI_QUIT_TIMEOUT_SECONDS
            try:
                driver.quit()
            except _SESSION_ERRORS:
                logger.debug("Ending the Safari session failed", exc_info=True)
        self._drop()
        self._session_version = ""
        self._unanswered = False

    def open(self, url: str | None) -> WebOpenResult:
        attached = self.is_open()
        if not attached:
            if not self.is_installed():
                raise BrowserNotInstalledError("Browser not installed")
            service = SafariService(executable_path=str(_SAFARIDRIVER))
            try:
                driver = _start_session(
                    service,
                    SafariOptions(),
                    lambda url: SafariRemoteConnection(
                        remote_server_addr=url, client_config=_client_config(url)
                    ),
                )
            except SessionNotCreatedException as e:
                message = (e.msg or "").lower()
                if any(marker in message for marker in _SAFARI_SETUP_MARKERS):
                    raise WebBrowserSetupNeededError(
                        WebBrowserSetupStep.SAFARI_REMOTE_AUTOMATION_OFF
                    ) from e
                raise ExecutionError("Could not start the browser") from e
            except _SESSION_ERRORS as e:
                logger.debug("Starting Safari failed", exc_info=True)
                raise ExecutionError("Could not start the browser") from e
            self._adopt(driver, service)
        if url is not None:
            self.navigate(url)
        return WebOpenResult(
            mode=WebBrowserMode.PAGE, version=self.version(), attached=attached
        )

    def close(self) -> str:
        self._quit()
        return "Closed the browser"

    def shutdown(self) -> None:
        self._quit()

    def cleanup(self) -> None:
        """Quit the runner's Safari session only if there is one."""
        if self._driver is None:
            return
        try:
            self._quit()
        except Exception:  # noqa: BLE001 - cleanup must never raise
            logger.debug("Safari cleanup failed", exc_info=True)


def make_desktop_backends(
    context: WebBrowserContext,
) -> dict[WebBrowser, WebBrowserBackend]:
    """One backend per desktop browser; the hub keeps those in the target's
    table."""
    return {
        WebBrowser.CHROME: ChromiumDesktopBackend(WebBrowser.CHROME, context),
        WebBrowser.EDGE: ChromiumDesktopBackend(WebBrowser.EDGE, context),
        WebBrowser.FIREFOX: FirefoxDesktopBackend(WebBrowser.FIREFOX, context),
        WebBrowser.SAFARI: SafariDesktopBackend(context),
    }
