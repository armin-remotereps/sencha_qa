"""The web browser layer's one owner (WEB_BROWSER_V1).

``client.py`` hands every ``web_browser_*`` message to ``WebBrowserHub``. The
hub parses it, builds the backends of this runner's target at the first
message (only the browsers in ``web_browsers_for(target, host_os)``), picks
the browser a request acts on and runs the call on its own worker thread,
``web-browser`` (separate from the Playwright thread), within the runner's
answer budget (phones: WEB_BROWSER_PHONE_ACTION_BUDGET_SECONDS, since the
phone session has its own budget after a presence check).

Which browser (every message but open and list): named -> that one if it is
open, else WebBrowserNotOpenError(named); not named -> the single open one;
none -> WebBrowserNotOpenError(""); several -> WebBrowserAmbiguousError.
Presence (``backend.is_open()``) is cached for 2 s per browser.

Errors: a job's ExecutionError / ProtocolError pass through; anything else
(WebDriver, urllib3, OS errors) becomes one fixed ExecutionError, and the
original is logged at warning level as its class and a cut first line only.

Stuck calls: a blocking WebDriver call cannot be interrupted (desktop
sessions bound each request with an HTTP timeout below the budget). A call
past its budget answers a retryable error and keeps its thread; the browser
it was touching is then busy: calls on it, and calls that name no browser
(their choice would touch it), get the fixed busy error until the stuck call
returns (on a phone: every browser, as they share one phone session). The
hub does nothing else to it: ending its session would quit a browser the
session owns (Safari, Android Firefox); attach backends drop a session that
hit their HTTP timeout themselves. A call whose handler is cancelled is set
aside the same way, and a set-aside call that had not started never runs.
Later calls run on a new thread; at most ``_MAX_STUCK_CALLS`` stuck threads
exist (cleanup and shutdown count, and are skipped at the bound), beyond
that every call is refused. Worker threads are daemon threads, so a stuck call never holds up
the runner's exit or a self-update hand-over.
"""

import asyncio
import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Final, TypeVar

from controller_client.exceptions import (
    BrowserNotInstalledError,
    ExecutionError,
    ProtocolError,
    WebBrowserAmbiguousError,
    WebBrowserNotOpenError,
    WebBrowserUnsupportedError,
)
from controller_client.protocol import (
    MOBILE_TARGETS,
    WEB_BROWSER_ACTION_BUDGET_SECONDS,
    WEB_BROWSER_DISPLAY_NAMES,
    WEB_BROWSER_OPEN_BUDGET_SECONDS,
    WEB_BROWSER_PHONE_ACTION_BUDGET_SECONDS,
    MessageType,
    RunnerTarget,
    WebBrowser,
    WebBrowserListEntry,
    WebBrowserMode,
    WebBrowserResultPayload,
    ordered_web_browsers,
    parse_web_browser_act_payload,
    parse_web_browser_choice_payload,
    parse_web_browser_elements_payload,
    parse_web_browser_evaluate_payload,
    parse_web_browser_history_payload,
    parse_web_browser_navigate_payload,
    parse_web_browser_open_payload,
    parse_web_browser_read_payload,
    parse_web_browser_tabs_payload,
    parse_web_browser_wait_payload,
    web_browsers_for,
    web_url_is_file,
)
from controller_client.web_browser_android import make_android_backends
from controller_client.web_browser_backend import (
    BackendFactory,
    WebActRequest,
    WebBrowserBackend,
    WebBrowserContext,
    WebReadRequest,
)
from controller_client.web_browser_desktop import make_desktop_backends
from controller_client.web_browser_ios import make_ios_backends

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

WORKER_THREAD_NAME: Final[str] = "web-browser"
_PRESENCE_TTL_SECONDS: Final[float] = 2.0
# Environment cleanup and runner stop: well inside the server's cleanup
# timeout, which covers the whole environment cleanup.
_LIFECYCLE_BUDGET_SECONDS: Final[float] = 9.0
# Stuck calls whose threads may still be running; beyond, every call waits.
_MAX_STUCK_CALLS: Final[int] = 2
_LOG_MESSAGE_MAX_CHARS: Final[int] = 120
_BUDGET_TEXT: Final[str] = "The browser did not answer in time; try again"
_BUSY_TEXT: Final[str] = (
    "The browser is still busy with an earlier action; try again shortly"
)
_FAILED_TEXT: Final[str] = "The browser action failed on the test machine"
_FILE_URL_TEXT: Final[str] = "file URLs are not allowed on phones"
_FACTORIES: Final[dict[RunnerTarget, BackendFactory]] = {
    RunnerTarget.DESKTOP: make_desktop_backends,
    RunnerTarget.ANDROID: make_android_backends,
    RunnerTarget.IOS: make_ios_backends,
}


@dataclass(frozen=True)
class _Outcome:
    content: str
    browser: WebBrowser | None
    mode: WebBrowserMode | None
    truncated: bool = False
    browsers: tuple[WebBrowserListEntry, ...] = ()


class _Ticket:
    """One call on a worker thread: the browser it is touching (None before
    it touches one) and whether the hub stopped waiting for it."""

    def __init__(self) -> None:
        self.browser: WebBrowser | None = None
        self._lock = threading.Lock()
        self._abandoned = False
        self._finished = False

    def begin(self) -> bool:
        """False (and finished) when the call was abandoned before it ran."""
        with self._lock:
            if self._abandoned:
                self._finished = True
                return False
            return True

    def abandon(self) -> bool:
        """Marks the call stuck; False when it finished in the meantime."""
        with self._lock:
            if self._finished:
                return False
            self._abandoned = True
            return True

    def finish(self) -> None:
        with self._lock:
            self._finished = True

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._finished


class _Worker:
    """One daemon thread running jobs in order. Daemon, so a job stuck in a
    WebDriver request never blocks the runner's exit or a self-update."""

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue[Callable[[], None] | None] = queue.SimpleQueue()
        threading.Thread(
            target=self._loop, name=WORKER_THREAD_NAME, daemon=True
        ).start()

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            job()

    def submit(self, job: Callable[[], _T]) -> "Future[_T]":
        future: Future[_T] = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(job())
            except Exception as e:  # noqa: BLE001 - handed to the waiter
                future.set_exception(e)

        self._jobs.put(run)
        return future

    def retire(self) -> None:
        """The thread exits after the job it is running (if any)."""
        self._jobs.put(None)


def _discard(future: "asyncio.Future[_T]") -> None:
    if not future.cancelled():
        future.exception()


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:_LOG_MESSAGE_MAX_CHARS] if text else ""


class WebBrowserHub:
    def __init__(self, context: WebBrowserContext) -> None:
        self._context = context
        self._backends: dict[WebBrowser, WebBrowserBackend] | None = None
        self._worker: _Worker | None = None
        self._stuck: list[_Ticket] = []
        self._presence: dict[WebBrowser, tuple[float, bool]] = {}

    # --- worker thread ------------------------------------------------------

    def _current_worker(self) -> _Worker:
        if self._worker is None:
            self._worker = _Worker()
        return self._worker

    def _prune_stuck(self) -> None:
        self._stuck = [ticket for ticket in self._stuck if not ticket.finished]

    def _refuse_if_busy(self, named: WebBrowser | None) -> None:
        """The busy error while a stuck call holds the browser this request
        may touch (``named``, or every browser when none is named; on a
        phone every browser, since they share one phone session)."""
        self._prune_stuck()
        if not self._stuck:
            return
        busy = {ticket.browser for ticket in self._stuck}
        if (
            len(self._stuck) >= _MAX_STUCK_CALLS
            or self._context.target in MOBILE_TARGETS
            or None in busy
            or named is None
            or named in busy
        ):
            raise ExecutionError(_BUSY_TEXT)

    def _run_job(self, job: Callable[[_Ticket], _T], ticket: _Ticket) -> _T:
        if not ticket.begin():
            # Abandoned while queued: the request was already answered.
            raise ExecutionError(_BUDGET_TEXT)
        try:
            return job(ticket)
        except (ExecutionError, ProtocolError):
            raise
        except Exception as e:
            logger.warning("Browser action failed: %s: %s", type(e).__name__, _short(e))
            raise ExecutionError(_FAILED_TEXT) from e
        finally:
            ticket.finish()

    def _set_aside(
        self, worker: _Worker, waiter: "asyncio.Future[_T]", ticket: _Ticket
    ) -> bool:
        """Leave a call the hub stopped waiting for on its thread (later
        calls get a new one); False when it already finished."""
        if not ticket.abandon():
            return False
        # Its late result is read by nobody (the waiter is never cancelled).
        waiter.add_done_callback(_discard)
        if self._worker is worker:
            self._worker = None
        worker.retire()
        self._stuck.append(ticket)
        self._presence.clear()
        return True

    async def _call(self, job: Callable[[_Ticket], _T], budget: float) -> _T:
        ticket = _Ticket()
        worker = self._current_worker()
        waiter = asyncio.wrap_future(worker.submit(lambda: self._run_job(job, ticket)))
        try:
            # asyncio.wait never raises the job's own TimeoutError: a budget
            # timeout is only "the waiter is not done".
            done, _pending = await asyncio.wait({waiter}, timeout=budget)
        except asyncio.CancelledError:
            # The handler was cancelled (socket closed): same as a timeout,
            # so the next call neither queues behind it nor sees it run late.
            self._set_aside(worker, waiter, ticket)
            raise
        if not done and self._set_aside(worker, waiter, ticket):
            raise ExecutionError(_BUDGET_TEXT)
        return await waiter

    # --- backends and choice (worker thread) ----------------------------------

    def _all_backends(self) -> dict[WebBrowser, WebBrowserBackend]:
        if self._backends is None:
            served = web_browsers_for(self._context.target, self._context.host_os)
            made = _FACTORIES[self._context.target](self._context)
            self._backends = {
                browser: backend
                for browser, backend in made.items()
                if browser in served
            }
        return self._backends

    def _is_open(
        self, ticket: _Ticket, browser: WebBrowser, backend: WebBrowserBackend
    ) -> bool:
        now = time.monotonic()
        cached = self._presence.get(browser)
        if cached is not None and now - cached[0] < _PRESENCE_TTL_SECONDS:
            return cached[1]
        ticket.browser = browser
        try:
            is_open = backend.is_open()
        except Exception:  # noqa: BLE001 - a failing probe means "not open"
            logger.debug("Presence check of %s failed", browser.value, exc_info=True)
            is_open = False
        self._presence[browser] = (now, is_open)
        return is_open

    def _forget(self, browser: WebBrowser) -> None:
        self._presence.pop(browser, None)

    def _choose(
        self, ticket: _Ticket, named: WebBrowser | None
    ) -> tuple[WebBrowser, WebBrowserBackend]:
        backends = self._all_backends()
        if named is not None:
            backend = backends.get(named)
            if backend is None:
                raise WebBrowserUnsupportedError("This browser is not available here")
            if not self._is_open(ticket, named, backend):
                raise WebBrowserNotOpenError(named.value)
            ticket.browser = named
            return named, backend
        open_ones = [
            browser
            for browser in ordered_web_browsers(frozenset(backends))
            if self._is_open(ticket, browser, backends[browser])
        ]
        if not open_ones:
            raise WebBrowserNotOpenError("")
        if len(open_ones) > 1:
            raise WebBrowserAmbiguousError(
                tuple(browser.value for browser in open_ones)
            )
        ticket.browser = open_ones[0]
        return open_ones[0], backends[open_ones[0]]

    def _on_chosen(
        self,
        ticket: _Ticket,
        named: WebBrowser | None,
        operation: Callable[[WebBrowserBackend], str],
    ) -> _Outcome:
        browser, backend = self._choose(ticket, named)
        try:
            content = operation(backend)
        except WebBrowserNotOpenError:
            self._forget(browser)
            raise
        return _Outcome(content=content, browser=browser, mode=backend.mode())

    def _open(self, ticket: _Ticket, browser: WebBrowser, url: str | None) -> _Outcome:
        backend = self._all_backends().get(browser)
        if backend is None:
            raise WebBrowserUnsupportedError("This browser is not available here")
        ticket.browser = browser
        if not backend.is_installed():
            raise BrowserNotInstalledError("Browser not installed")
        self._forget(browser)
        try:
            result = backend.open(url)
        finally:
            self._forget(browser)
        verb = "Attached to" if result.attached else "Opened"
        version = f" {result.version}" if result.version else ""
        return _Outcome(
            content=f"{verb} {WEB_BROWSER_DISPLAY_NAMES[browser]}{version}",
            browser=browser,
            mode=result.mode,
        )

    def _list(self, ticket: _Ticket) -> _Outcome:
        backends = self._all_backends()
        entries: list[WebBrowserListEntry] = []
        lines: list[str] = []
        for browser in ordered_web_browsers(frozenset(backends)):
            backend = backends[browser]
            ticket.browser = browser
            installed = backend.is_installed()
            is_open = installed and self._is_open(ticket, browser, backend)
            mode = backend.mode() if is_open else None
            entry = WebBrowserListEntry(
                browser=browser,
                installed=installed,
                open=is_open,
                mode=mode,
                how_to_open=backend.how_to_open,
                version=backend.version() if installed else "",
            )
            entries.append(entry)
            state = "not installed"
            if installed:
                state = f"open ({mode.value})" if is_open and mode else "not open"
            lines.append(f"{browser.value}: {state}")
        ticket.browser = None
        return _Outcome(
            content="\n".join(lines),
            browser=None,
            mode=None,
            browsers=tuple(entries),
        )

    def _evaluate(
        self,
        ticket: _Ticket,
        named: WebBrowser | None,
        script: str,
        args_json: str,
        timeout_ms: int,
    ) -> _Outcome:
        browser, backend = self._choose(ticket, named)
        try:
            result = backend.evaluate(script, args_json, timeout_ms)
        except WebBrowserNotOpenError:
            self._forget(browser)
            raise
        return _Outcome(
            content=result.json_text,
            browser=browser,
            mode=backend.mode(),
            truncated=result.truncated,
        )

    def _close(self, ticket: _Ticket, named: WebBrowser | None) -> _Outcome:
        browser, backend = self._choose(ticket, named)
        try:
            content = backend.close()
        finally:
            self._forget(browser)
        return _Outcome(content=content, browser=browser, mode=None)

    # --- messages -------------------------------------------------------------

    def _refuse_phone_file_url(self, url: str | None) -> None:
        if (
            url is not None
            and self._context.target in MOBILE_TARGETS
            and web_url_is_file(url)
        ):
            raise ProtocolError(_FILE_URL_TEXT)

    def _job(
        self, message_type: MessageType, data: dict[str, object]
    ) -> tuple[WebBrowser | None, Callable[[_Ticket], _Outcome]]:
        """Parses ``data`` (ProtocolError on bad input) into the browser it
        names (None: not named, or list) and the call to run on the worker
        thread."""
        if message_type is MessageType.WEB_BROWSER_OPEN:
            opening = parse_web_browser_open_payload(data)
            self._refuse_phone_file_url(opening.url)
            return opening.browser, lambda ticket: self._open(
                ticket, opening.browser, opening.url
            )
        if message_type is MessageType.WEB_BROWSER_LIST:
            return None, self._list
        if message_type is MessageType.WEB_BROWSER_NAVIGATE:
            navigate = parse_web_browser_navigate_payload(data)
            self._refuse_phone_file_url(navigate.url)
            return navigate.browser, lambda ticket: self._on_chosen(
                ticket,
                navigate.browser,
                lambda backend: backend.navigate(navigate.url),
            )
        if message_type is MessageType.WEB_BROWSER_HISTORY:
            history = parse_web_browser_history_payload(data)
            return history.browser, lambda ticket: self._on_chosen(
                ticket,
                history.browser,
                lambda backend: backend.history(history.action),
            )
        if message_type is MessageType.WEB_BROWSER_TABS:
            tabs = parse_web_browser_tabs_payload(data)
            return tabs.browser, lambda ticket: self._on_chosen(
                ticket,
                tabs.browser,
                lambda backend: backend.tabs(tabs.action, tabs.index),
            )
        if message_type is MessageType.WEB_BROWSER_ELEMENTS:
            listing = parse_web_browser_elements_payload(data)
            return listing.browser, lambda ticket: self._on_chosen(
                ticket,
                listing.browser,
                lambda backend: backend.elements(
                    listing.scope_css, listing.max_elements
                ),
            )
        if message_type is MessageType.WEB_BROWSER_ACT:
            act = parse_web_browser_act_payload(data)
            request = WebActRequest(
                action=act.action,
                target=act.target,
                text=act.text,
                clear=act.clear,
                value=act.value,
                label=act.label,
                key=act.key,
            )
            return act.browser, lambda ticket: self._on_chosen(
                ticket, act.browser, lambda backend: backend.act(request)
            )
        if message_type is MessageType.WEB_BROWSER_READ:
            read = parse_web_browser_read_payload(data)
            read_request = WebReadRequest(
                what=read.what,
                target=read.target,
                name=read.name,
                max_chars=read.max_chars,
            )
            return read.browser, lambda ticket: self._on_chosen(
                ticket, read.browser, lambda backend: backend.read(read_request)
            )
        if message_type is MessageType.WEB_BROWSER_WAIT:
            wait = parse_web_browser_wait_payload(data)
            return wait.browser, lambda ticket: self._on_chosen(
                ticket,
                wait.browser,
                lambda backend: backend.wait(wait.target, wait.state, wait.timeout_ms),
            )
        if message_type is MessageType.WEB_BROWSER_EVALUATE:
            evaluate = parse_web_browser_evaluate_payload(data)
            return evaluate.browser, lambda ticket: self._evaluate(
                ticket,
                evaluate.browser,
                evaluate.script,
                evaluate.args_json,
                evaluate.timeout_ms,
            )
        if message_type is MessageType.WEB_BROWSER_CLOSE:
            closing = parse_web_browser_choice_payload(data)
            return closing.browser, lambda ticket: self._close(ticket, closing.browser)
        raise ProtocolError("Not a web browser message")

    def _budget(self, message_type: MessageType) -> float:
        if message_type is MessageType.WEB_BROWSER_OPEN:
            return float(WEB_BROWSER_OPEN_BUDGET_SECONDS)
        if self._context.target in MOBILE_TARGETS:
            return float(WEB_BROWSER_PHONE_ACTION_BUDGET_SECONDS)
        return float(WEB_BROWSER_ACTION_BUDGET_SECONDS)

    async def handle(
        self, message_type: MessageType, data: dict[str, object]
    ) -> WebBrowserResultPayload:
        """Runs one ``web_browser_*`` request; raises ProtocolError on bad
        input and ExecutionError (or a web browser error of ``exceptions``)
        otherwise, never another exception."""
        start = time.monotonic()
        named, job = self._job(message_type, data)
        self._refuse_if_busy(named)
        outcome = await self._call(job, self._budget(message_type))
        return WebBrowserResultPayload(
            success=True,
            content=outcome.content,
            browser=outcome.browser,
            mode=outcome.mode,
            truncated=outcome.truncated,
            browsers=outcome.browsers,
            duration_ms=(time.monotonic() - start) * 1000,
        )

    # --- lifecycle --------------------------------------------------------------

    def _each_backend(
        self, ticket: _Ticket, step: Callable[[WebBrowserBackend], None]
    ) -> None:
        for browser, backend in (self._backends or {}).items():
            ticket.browser = browser
            try:
                step(backend)
            except Exception:  # noqa: BLE001 - lifecycle steps never raise
                logger.debug(
                    "Browser %s did not stop cleanly", browser.value, exc_info=True
                )
        ticket.browser = None
        self._presence.clear()

    def _cleanup_all(self, ticket: _Ticket) -> None:
        self._all_backends()
        self._each_backend(ticket, lambda backend: backend.cleanup())

    async def shutdown(self) -> None:
        """Runner stop / self-update: end every driver session (person-opened
        browsers stay open), then retire the worker thread. Runs even while a
        call is stuck: ending a session makes its blocked request fail."""
        self._prune_stuck()
        if len(self._stuck) >= _MAX_STUCK_CALLS:
            logger.warning("Browser sessions not ended: earlier calls still stuck")
        elif self._backends is not None:
            try:
                await self._call(
                    lambda ticket: self._each_backend(
                        ticket, lambda backend: backend.shutdown()
                    ),
                    _LIFECYCLE_BUDGET_SECONDS,
                )
            except ExecutionError:
                logger.warning("Browser sessions did not end in time")
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.retire()

    async def cleanup(self) -> None:
        """Environment cleanup: every backend's ``cleanup`` (ends the
        browsers the runner owns without attaching, including test browsers
        a person opened before any browser message arrived). Runs even while
        a call is stuck, within a few seconds; never raises."""
        self._prune_stuck()
        if len(self._stuck) >= _MAX_STUCK_CALLS:
            logger.warning("Browser cleanup skipped: earlier calls still stuck")
            return
        try:
            await self._call(self._cleanup_all, _LIFECYCLE_BUDGET_SECONDS)
        except ExecutionError:
            logger.warning("Browser cleanup did not finish in time")
