"""iOS browsers of the web browser layer: Safari, Chrome, Edge and Firefox.

Everything runs through the phone's one XCUITest session (``web_call``:
action lock, budget, a lost session recreated). Per operation: bring the
app to the front, pick the web view context that belongs to its bundle id
(``mobile: getContexts`` with the details ``enableWebviewDetailsCollection``
adds), switch to it, run the ``web_browser_page_ops`` operation and ALWAYS
switch back to NATIVE_APP (the switch itself sits inside the guarded part,
since a timed-out switch may still have happened). When the switch back
fails, the session is reported lost, so the phone session recreates it
(it starts in NATIVE_APP); the operation is not run again then. Driver
errors become short fixed texts. The context list is read on
every operation, so the mode is detected again each time (open and navigate
included): a person who turns Web Inspector on needs no restart.

No inspectable context for the app -> accessibility mode
(``web_browser_accessibility``): links, buttons, texts and fields of the
app's web view from the native page source; selectors, scripts, tabs,
history, select, wait and hover are refused there. Telling the setup steps
apart (a heuristic; Unverified on devices):

- Safari: no page context -> IOS_WEB_INSPECTOR_OFF (the device setting).
- Chrome: when another app's web view is listed, or the device is a
  simulator, Web Inspector is on for the device, so Chrome's own setting
  is off -> CHROME_IOS_WEB_INSPECTOR_OFF; otherwise the device setting is
  the first suspect -> IOS_WEB_INSPECTOR_OFF.
- Edge and Firefox: their builds decide whether pages are inspectable, so
  there is nothing to set up -> WEB_BROWSER_UNSUPPORTED.

Opening launches the app through the phone backend (so the device cleanup
closes it); an app already running is attached to. ``close`` only detaches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from appium.webdriver.applicationstate import ApplicationState
from appium.webdriver.webdriver import WebDriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchDriverException,
    WebDriverException,
)
from selenium.webdriver.common.keys import Keys
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from controller_client import mobile_gestures as gestures
from controller_client import web_browser_accessibility as a11y
from controller_client.exceptions import (
    ExecutionError,
    WebBrowserNotOpenError,
    WebBrowserSetupNeededError,
    WebBrowserUnsupportedError,
)
from controller_client.mobile_device import MobileDevice
from controller_client.mobile_ios import (
    IOS_CHROME_BUNDLE_ID,
    IOS_EDGE_BUNDLE_ID,
    IOS_FIREFOX_BUNDLE_ID,
    IOS_SAFARI_BUNDLE_ID,
)
from controller_client.protocol import (
    DeviceKind,
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
from controller_client.web_browser_evaluate import evaluate_script
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
from controller_client.web_browser_presence_mobile import IosPresence

logger = logging.getLogger(__name__)

_BUNDLE_IDS: Final[dict[WebBrowser, str]] = {
    WebBrowser.SAFARI: IOS_SAFARI_BUNDLE_ID,
    WebBrowser.CHROME: IOS_CHROME_BUNDLE_ID,
    WebBrowser.EDGE: IOS_EDGE_BUNDLE_ID,
    WebBrowser.FIREFOX: IOS_FIREFOX_BUNDLE_ID,
}
_NATIVE: Final[str] = "NATIVE_APP"
_WEBVIEW_PREFIX: Final[str] = "WEBVIEW"
# How long ``open`` lets a just-launched app's page appear; other calls
# take the list as it is.
_OPEN_WEBVIEW_WAIT_MS: Final[int] = 3_000
_NAVIGATE_WEBVIEW_WAIT_MS: Final[int] = 1_500
_RESTORE_ATTEMPTS: Final[int] = 2

_PAGE_ONLY: Final[str] = "Not available in accessibility mode"
_NO_HOVER: Final[str] = "A phone has no pointer to hover with"
_NO_FILE_URLS: Final[str] = "File URLs cannot be opened on a phone"
_UNKNOWN_KEY: Final[str] = "The key cannot be pressed on the phone"
_NATIVE_LOST: Final[str] = "The phone could not return to the app view"
_SESSION_RESET: Final[str] = (
    "The phone's automation session was restarted; check the page before "
    "trying again"
)
_FAILED: Final[str] = "The browser could not carry out the action"
_DETACHED: Final[str] = "Detached from the browser; the app stays open"
_IOS_KEYS: Final[dict[str, str]] = {
    "enter": Keys.ENTER,
    "return": Keys.ENTER,
    "tab": Keys.TAB,
    "backspace": Keys.BACKSPACE,
    "delete": Keys.DELETE,
    "space": " ",
}


def make_ios_backends(
    context: WebBrowserContext,
) -> dict[WebBrowser, WebBrowserBackend]:
    mobile = context.mobile
    if mobile is None:
        return {}
    presence = IosPresence(mobile, tuple(_BUNDLE_IDS.values()))
    return {
        browser: IosWebBrowserBackend(browser, bundle_id, mobile, presence)
        for browser, bundle_id in _BUNDLE_IDS.items()
    }


@dataclass(frozen=True)
class _Context:
    name: str
    # None when the driver gives no details (older XCUITest drivers).
    bundle_id: str | None


def _ios_press_key(driver: WebDriver, key: str) -> None:
    value = _IOS_KEYS.get(key.strip().lower())
    if value is None:
        raise ExecutionError(_UNKNOWN_KEY)
    gestures.send_keys(driver, [value], 0.0)


def _check_url(url: str) -> None:
    if web_url_is_file(url):
        raise WebBrowserUnsupportedError(_NO_FILE_URLS)


def _uses_selector(target: WebElementTarget | None) -> bool:
    return target is not None and (target.css is not None or target.xpath is not None)


def _web_contexts(driver: WebDriver, wait_ms: int) -> list[_Context]:
    """The session's web view contexts, with bundle ids when known."""
    try:
        raw = driver.execute_script(
            "mobile: getContexts", {"waitForWebviewMs": wait_ms}
        )
    except (InvalidSessionIdException, NoSuchDriverException):
        raise
    except WebDriverException:
        names = driver.contexts
        return [
            _Context(name, None)
            for name in names
            if isinstance(name, str) and name.startswith(_WEBVIEW_PREFIX)
        ]
    contexts: list[_Context] = []
    for entry in raw if isinstance(raw, list) else []:
        name: object = None
        bundle: str | None = None
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, Mapping):
            name = entry.get("id")
            value = entry.get("bundleId")
            bundle = value if isinstance(value, str) and value else None
        if isinstance(name, str) and name.startswith(_WEBVIEW_PREFIX):
            contexts.append(_Context(name, bundle))
    return contexts


_RESTORE_ERRORS: Final = (WebDriverException, Urllib3HTTPError, OSError)


def _restore_native(driver: WebDriver, cause: BaseException | None) -> None:
    """Back to the native context, so screenshots and taps work again.

    When that fails, raises InvalidSessionIdException (chained to ``cause``,
    the operation's own error, when there is one): the phone session then
    recreates the session, which starts in NATIVE_APP.
    """
    for _ in range(_RESTORE_ATTEMPTS):
        try:
            driver.switch_to.context(_NATIVE)
            return
        except (InvalidSessionIdException, NoSuchDriverException):
            # The session is gone; its replacement starts in NATIVE_APP.
            return
        except _RESTORE_ERRORS as exc:
            logger.warning("Switching back to the native context failed: %s", exc)
    if cause is not None:
        logger.warning("The operation before it failed: %s", cause)
    raise InvalidSessionIdException(_NATIVE_LOST) from cause


def _fixed_text[T](operation: Callable[[WebDriver], T], driver: WebDriver) -> T:
    """Run ``operation``; a driver error other than a lost session becomes a
    short fixed text (the phone session would pass its raw text on)."""
    try:
        return operation(driver)
    except (InvalidSessionIdException, NoSuchDriverException):
        raise
    except WebDriverException as exc:
        logger.warning("Browser action on the phone failed: %s", exc.msg)
        raise ExecutionError(_FAILED) from exc


class IosWebBrowserBackend:
    def __init__(
        self,
        browser: WebBrowser,
        bundle_id: str,
        mobile: MobileDevice,
        presence: IosPresence,
    ) -> None:
        self._browser = browser
        self._bundle_id = bundle_id
        self._mobile = mobile
        self._presence = presence
        self._listing = PageListing()
        self._a11y = a11y.A11yListing()
        self._mode: WebBrowserMode | None = None
        # The page context last used: kept while it exists (a tab switch).
        self._context: str | None = None
        self._other_web_views = False
        self._installed = False
        self._version: str | None = None

    @property
    def browser(self) -> WebBrowser:
        return self._browser

    @property
    def how_to_open(self) -> WebBrowserHowToOpen:
        return WebBrowserHowToOpen.APP

    def is_installed(self) -> bool:
        if not self._installed:
            found = self._mobile.is_app_installed(self._bundle_id)
            self._installed = found is not None
        return self._installed

    def version(self) -> str:
        if self._version is None:
            try:
                self._version = self._mobile.web_call(self._read_version)
            except ExecutionError as exc:
                logger.debug("Reading the browser version failed: %s", exc)
                return ""
        return self._version

    def is_open(self) -> bool:
        return self._presence.is_running(self._bundle_id)

    def mode(self) -> WebBrowserMode | None:
        return self._mode if self.is_open() else None

    def open(self, url: str | None) -> WebOpenResult:
        if url is not None:
            _check_url(url)
        self._presence.invalidate()
        attached = self.is_open()
        if not attached:
            self._mobile.launch_app(self._bundle_id)
            self._presence.invalidate()
        self._forget()
        if url is None:
            self._run(lambda driver: "", lambda driver: "", _OPEN_WEBVIEW_WAIT_MS)
        else:
            self._navigate(url, _OPEN_WEBVIEW_WAIT_MS)
        return WebOpenResult(
            mode=self._mode or WebBrowserMode.ACCESSIBILITY,
            version=self.version(),
            attached=attached,
        )

    def navigate(self, url: str) -> str:
        _check_url(url)
        return self._navigate(url, 0)

    def history(self, action: WebHistoryAction) -> str:
        return self._run(
            lambda driver: page_history(driver, self._listing, action),
            self._page_only,
        )

    def tabs(self, action: WebTabsAction, index: int | None) -> str:
        def in_page(driver: WebDriver) -> str:
            result = page_tabs(driver, self._listing, action, index)
            # A switch may move to another page context: keep using it.
            current = driver.current_context
            if isinstance(current, str) and current.startswith(_WEBVIEW_PREFIX):
                self._context = current
            return result

        return self._run(in_page, self._page_only)

    def elements(self, scope_css: str | None, max_elements: int) -> str:
        def accessible(driver: WebDriver) -> str:
            if scope_css is not None:
                raise self._page_only_error()
            page = a11y.parse_ios_page(driver.page_source)
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

        def accessible(driver: WebDriver) -> str:
            if _uses_selector(request.target) or request.action is WebActKind.SELECT:
                raise self._page_only_error()
            return a11y.act(driver, self._a11y, request, _ios_press_key)

        return self._run(
            lambda driver: page_act(driver, self._listing, request, allow_hover=False),
            accessible,
        )

    def read(self, request: WebReadRequest) -> str:
        def accessible(driver: WebDriver) -> str:
            if _uses_selector(request.target) or (
                request.what is WebReadWhat.ATTRIBUTE
            ):
                raise self._page_only_error()
            page = a11y.parse_ios_page(driver.page_source)
            return a11y.read(page, self._a11y, request)

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
        def accessible(driver: WebDriver) -> WebEvalResult:
            raise self._page_only_error()

        return self._run(
            lambda driver: evaluate_script(driver, script, args_json, timeout_ms),
            accessible,
        )

    def close(self) -> str:
        self._forget()
        self._mode = None
        return _DETACHED

    def shutdown(self) -> None:
        self._forget()

    def cleanup(self) -> None:
        # Phone apps are left to the device cleanup: detach only.
        self._forget()

    # --- helpers ----------------------------------------------------------

    def _forget(self) -> None:
        self._listing.reset()
        self._a11y.reset()
        self._context = None

    def _navigate(self, url: str, wait_ms: int) -> str:
        def accessible(driver: WebDriver) -> str:
            # Unverified: XCUITest opens the URL in the app named by
            # ``bundleId`` (iOS 16.4+ XCUIApplication.open(url)).
            driver.execute_script(
                "mobile: deepLink", {"url": url, "bundleId": self._bundle_id}
            )
            self._a11y.reset()
            # The page may be inspectable once it has loaded.
            if self._choose(driver, _NAVIGATE_WEBVIEW_WAIT_MS) is not None:
                self._mode = WebBrowserMode.PAGE
            return url

        return self._run(
            lambda driver: page_navigate(driver, self._listing, url),
            accessible,
            wait_ms,
        )

    def _run[T](
        self,
        page_op: Callable[[WebDriver], T],
        a11y_op: Callable[[WebDriver], T],
        wait_ms: int = 0,
    ) -> T:
        acted = False

        def operation(driver: WebDriver) -> T:
            nonlocal acted
            if acted:
                # Called again on a recreated session: the first attempt may
                # have acted already, so it is not repeated.
                self._forget()
                raise ExecutionError(_SESSION_RESET)
            self._bring_to_front(driver)
            context = self._choose(driver, wait_ms)
            acted = True
            if context is None:
                self._mode = WebBrowserMode.ACCESSIBILITY
                return a11y_op(driver)
            self._mode = WebBrowserMode.PAGE
            try:
                driver.switch_to.context(context)
                result = page_op(driver)
            except BaseException as exc:
                _restore_native(driver, exc)
                raise
            _restore_native(driver, None)
            return result

        return self._mobile.web_call(lambda driver: _fixed_text(operation, driver))

    def _bring_to_front(self, driver: WebDriver) -> None:
        state = driver.query_app_state(self._bundle_id)
        if state not in (
            ApplicationState.RUNNING_IN_BACKGROUND_SUSPENDED,
            ApplicationState.RUNNING_IN_BACKGROUND,
            ApplicationState.RUNNING_IN_FOREGROUND,
        ):
            self._presence.invalidate()
            self._forget()
            raise WebBrowserNotOpenError(self._browser.value)
        if state != ApplicationState.RUNNING_IN_FOREGROUND:
            driver.activate_app(self._bundle_id)

    def _choose(self, driver: WebDriver, wait_ms: int) -> str | None:
        contexts = _web_contexts(driver, wait_ms)
        if contexts and all(context.bundle_id is None for context in contexts):
            # Unverified fallback for drivers without details: the app is in
            # front, so its pages are taken to be the listed ones.
            mine = [context.name for context in contexts]
            self._other_web_views = False
        else:
            mine = [c.name for c in contexts if c.bundle_id == self._bundle_id]
            self._other_web_views = any(
                c.bundle_id is not None and c.bundle_id != self._bundle_id
                for c in contexts
            )
        if not mine:
            return None
        if self._context in mine:
            return self._context
        # Unverified: the last listed page of the app is its visible tab.
        self._context = mine[-1]
        self._listing.reset()
        return self._context

    def _page_only_error(self) -> ExecutionError:
        if self._browser is WebBrowser.SAFARI:
            return WebBrowserSetupNeededError(WebBrowserSetupStep.IOS_WEB_INSPECTOR_OFF)
        if self._browser is WebBrowser.CHROME:
            simulator = self._mobile.device_info().kind == DeviceKind.SIMULATOR.value
            step = (
                WebBrowserSetupStep.CHROME_IOS_WEB_INSPECTOR_OFF
                if self._other_web_views or simulator
                else WebBrowserSetupStep.IOS_WEB_INSPECTOR_OFF
            )
            return WebBrowserSetupNeededError(step)
        return WebBrowserUnsupportedError(_PAGE_ONLY)

    def _page_only(self, driver: WebDriver) -> str:
        raise self._page_only_error()

    def _read_version(self, driver: WebDriver) -> str:
        """CFBundleShortVersionString from ``mobile: listApps``, or ""."""
        app_type = "System" if self._browser is WebBrowser.SAFARI else "User"
        try:
            raw = driver.execute_script(
                "mobile: listApps",
                {
                    "applicationType": app_type,
                    "returnAttributes": [
                        "CFBundleIdentifier",
                        "CFBundleShortVersionString",
                    ],
                },
            )
        except (InvalidSessionIdException, NoSuchDriverException):
            raise
        except WebDriverException:
            return ""
        info = raw.get(self._bundle_id) if isinstance(raw, Mapping) else None
        value = (
            info.get("CFBundleShortVersionString")
            if isinstance(info, Mapping)
            else None
        )
        return value if isinstance(value, str) else ""
