"""The web browser layer's backend interface (WEB_BROWSER_V1).

One backend serves one browser on this runner (desktop Chrome, Android
Firefox, iOS Safari, ...). ``web_browser_hub.WebBrowserHub`` owns them, picks
the one a request acts on and runs every call on its own worker thread, so a
backend's methods are plain blocking calls and never run concurrently.

Backends never import Playwright or ``browser_executor``. Page-mode backends
share ``web_browser_page_ops`` (any Selenium/Appium ``WebDriver`` in a page
context) and ``web_browser_evaluate``.

Errors: raise the ``controller_client.exceptions`` web browser errors
(``WebBrowserSetupNeededError``, ``WebBrowserUnsupportedError``,
``WebBrowserNotOpenError``, ``WebScriptFailedError``), ``BrowserNotInstalledError``,
``BrowserElementNotFoundError`` or ``DeviceDisconnectedError``; anything else
becomes EXECUTION_FAILED.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from controller_client.mobile_device import MobileDevice
from controller_client.protocol import (
    RunnerTarget,
    WebActKind,
    WebBrowser,
    WebBrowserHowToOpen,
    WebBrowserMode,
    WebElementTarget,
    WebHistoryAction,
    WebReadWhat,
    WebTabsAction,
    WebWaitState,
)


@dataclass(frozen=True)
class WebBrowserContext:
    """What a backend factory gets from the hub.

    ``runner_root``: the Test Runner folder (holds ``web_profiles/``).
    ``mobile``: the phone backend on a phone target (its ``MobileSession``
    serialises every Appium call), else None. ``device_serial``: the adb
    serial / iOS UDID of the runner's device, else None.
    ``driver_download``: ``CONTROLLER_WEB_DRIVER_DOWNLOAD`` (Selenium Manager
    may download drivers).
    """

    target: RunnerTarget
    host_os: str
    runner_root: Path
    driver_download: bool
    mobile: MobileDevice | None
    device_serial: str | None


@dataclass(frozen=True)
class WebActRequest:
    action: WebActKind
    target: WebElementTarget | None
    text: str | None
    clear: bool
    value: str | None
    label: str | None
    key: str | None


@dataclass(frozen=True)
class WebReadRequest:
    what: WebReadWhat
    target: WebElementTarget | None
    name: str | None
    max_chars: int


@dataclass(frozen=True)
class WebEvalResult:
    """``json_text``: the result serialised to JSON on the runner, already cut
    to WEB_EVAL_RESULT_MAX_BYTES (``truncated`` then True)."""

    json_text: str
    truncated: bool


@dataclass(frozen=True)
class WebOpenResult:
    mode: WebBrowserMode
    version: str
    # True when the browser was already open and the backend attached.
    attached: bool


class WebBrowserBackend(Protocol):
    """One browser on this runner. All methods block; the hub serialises."""

    @property
    def browser(self) -> WebBrowser: ...

    @property
    def how_to_open(self) -> WebBrowserHowToOpen: ...

    def is_installed(self) -> bool:
        """Cheap; may be cached."""
        ...

    def version(self) -> str:
        """Installed version, or "" when unknown."""
        ...

    def is_open(self) -> bool:
        """Open and controllable right now (cheap; the hub caches 2 s)."""
        ...

    def mode(self) -> WebBrowserMode | None:
        """Mode of the open browser, or None when not open / not yet known."""
        ...

    def open(self, url: str | None) -> WebOpenResult:
        """Start the browser (or attach when already open), navigate to
        ``url`` when given."""
        ...

    def navigate(self, url: str) -> str:
        """Returns the URL after navigation."""
        ...

    def history(self, action: WebHistoryAction) -> str:
        """Returns the URL afterwards."""
        ...

    def tabs(self, action: WebTabsAction, index: int | None) -> str:
        """Returns a JSON list ``[{index, title, url, current}]`` afterwards."""
        ...

    def elements(self, scope_css: str | None, max_elements: int) -> str:
        """Lists interactive elements, one per line:
        ``[<index>] <tag/role> "<text≤100>" (<extra>)``, and remembers the
        listing so ``WebElementTarget.index`` refers to it."""
        ...

    def act(self, request: WebActRequest) -> str:
        """Returns a short confirmation."""
        ...

    def read(self, request: WebReadRequest) -> str: ...

    def wait(
        self, target: WebElementTarget, state: WebWaitState, timeout_ms: int
    ) -> str: ...

    def evaluate(
        self, script: str, args_json: str, timeout_ms: int
    ) -> WebEvalResult: ...

    def close(self) -> str:
        """Desktop launcher browsers: quit the process; session-owned
        (Safari, Android Firefox): end the session; phone apps: detach."""
        ...

    def shutdown(self) -> None:
        """End driver sessions without closing person-opened browsers
        (runner stop / self-update). Never raises."""
        ...

    def cleanup(self) -> None:
        """Environment cleanup: as ``close`` for browsers the runner owns
        (desktop launcher, Safari, Android Firefox); phone apps are left to
        the device cleanup. Never raises."""
        ...


BackendFactory = Callable[[WebBrowserContext], dict[WebBrowser, WebBrowserBackend]]
