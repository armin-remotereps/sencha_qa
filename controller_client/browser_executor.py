import base64
import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Download,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import (
    Frame,
    Locator,
    Page,
    Playwright,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import (
    sync_playwright,
)

from controller_client.exceptions import (
    BrowserElementNotFoundError,
    BrowserNotInstalledError,
    ExecutionError,
)
from controller_client.protocol import (
    BROWSER_DISPLAY_NAMES,
    BROWSER_ENGINES,
    ActionResultPayload,
    BrowserClickPayload,
    BrowserContentResultPayload,
    BrowserDownloadPayload,
    BrowserEngine,
    BrowserHoverPayload,
    BrowserName,
    BrowserNavigatePayload,
    BrowserTargetPayload,
    BrowserTypePayload,
    ScreenshotResponsePayload,
)

logger = logging.getLogger(__name__)

_LOGIN_INDICATORS: tuple[str, ...] = (
    "login",
    "log in",
    "sign in",
    "signin",
    "authenticate",
    "authorization",
    "sso",
    "cas/login",
    "oauth",
    "saml",
)

_DOWNLOAD_TIMEOUT_MS: int = 15000
# Well under the server's 30 s reply timeout, so a stuck action is reported
# back as a failure instead of the server giving up on the reply first.
_ACTION_TIMEOUT_MS: int = 10000


def _has_login_in_url(page: Page) -> bool:
    url_lower = page.url.lower()
    return any(indicator in url_lower for indicator in _LOGIN_INDICATORS)


def _has_login_in_title(page: Page) -> bool:
    try:
        title_lower = page.title().lower()
    except Exception:
        return False
    return any(indicator in title_lower for indicator in _LOGIN_INDICATORS)


def _detect_login_page(page: Page) -> str | None:
    if _has_login_in_url(page) or _has_login_in_title(page):
        return page.url
    return None


def _handle_download_timeout(page: Page) -> NoReturn:
    login_url = _detect_login_page(page)
    if login_url is not None:
        raise ExecutionError(
            f"Browser download failed: redirected to login page ({login_url})"
        )
    raise ExecutionError(
        f"Browser download failed: No download triggered. "
        f"Browser landed on: {page.url}"
    )


_COLLECT_ELEMENTS_JS = """
(startIndex) => {
    const SELECTORS = 'a, button, input, select, textarea, [role], [onclick], [tabindex]';

    // Walk into open shadow roots too: overlay close buttons are routinely
    // rendered inside a custom element, where querySelectorAll cannot see them.
    const candidates = [];
    const walk = (root) => {
        root.querySelectorAll('*').forEach(el => {
            // Drop the index stamped by an earlier collection, so a stale
            // attribute can never satisfy a selector meant for this one.
            if (el.hasAttribute('data-at-idx')) {
                el.removeAttribute('data-at-idx');
            }
            if (el.matches(SELECTORS)) {
                candidates.push(el);
            }
            if (el.shadowRoot) {
                walk(el.shadowRoot);
            }
        });
    };
    walk(document);

    const visible = candidates.filter(el => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && style.opacity !== '0'
            && rect.width > 0
            && rect.height > 0;
    });

    const result = [];
    visible.forEach((el, offset) => {
        const idx = startIndex + offset;
        el.setAttribute('data-at-idx', String(idx));

        const info = {
            idx: idx,
            tag: el.tagName.toLowerCase(),
            text: (el.textContent || '').trim().substring(0, 100),
            role: el.getAttribute('role') || '',
            ariaLabel: el.getAttribute('aria-label') || '',
            placeholder: el.getAttribute('placeholder') || '',
            type: el.getAttribute('type') || '',
            name: el.getAttribute('name') || '',
            id: el.id || '',
            href: el.getAttribute('href') || '',
            value: el.value !== undefined ? String(el.value).substring(0, 50) : '',
        };
        result.push(info);
    });

    return result;
}
"""


_LIST_DOWNLOADS_WAIT_S: float = 300.0
# Server waits 120 s for browser_download; the whole action, browser launch
# included, must finish inside this budget.
_EXPLICIT_DOWNLOAD_BUDGET_S: float = 110.0
# Each poll runs the Playwright event loop, which is what lets the download
# handlers finish their saves; sleeping instead would stall them.
_DOWNLOAD_POLL_MS: float = 250.0

# Playwright 1.50 launch errors that mean the browser is simply not on this
# machine: a bundled engine never downloaded ("Executable doesn't exist at"),
# a system channel not installed ("Chromium distribution 'msedge' is not found
# at"), or a browser this OS has no build of ("... is not supported on").
# Re-check these on every Playwright bump.
_NOT_INSTALLED_PATTERN: re.Pattern[str] = re.compile(
    r"Executable doesn't exist"
    r"|distribution '[^']*' is not found"
    r"|is not supported on"
)


# Channels of the Chromium engine that Playwright finds at the browser's normal
# system install path; `playwright install` would reinstall the system browser.
_SYSTEM_BROWSERS: frozenset[BrowserName] = frozenset(
    {BrowserName.CHROME, BrowserName.MSEDGE}
)


def browser_install_hint(browser: BrowserName) -> str:
    if browser in _SYSTEM_BROWSERS:
        return (
            f"Install {BROWSER_DISPLAY_NAMES[browser]} on the test machine the "
            "normal way; the Test Runner uses the system install."
        )
    return f"Run `playwright install {browser.value}` inside the Test Runner venv."


def launch_browser(
    playwright: Playwright,
    browser: BrowserName,
    *,
    headless: bool,
    channel: str | None = None,
) -> Browser:
    """Launch one of the supported browsers.

    ``chrome`` and ``msedge`` are channels of the Chromium engine. ``channel``
    overrides the channel for the bundled engines (diagnostics pass
    ``chromium`` so headless checks the same binary as a headed run).

    Raises BrowserNotInstalledError when the browser is missing; any other
    launch error is raised unchanged.
    """
    engine = BROWSER_ENGINES[browser]
    if engine is BrowserEngine.CHROMIUM:
        browser_type = playwright.chromium
        # Playwright already adds --no-sandbox (chromium_sandbox is off).
        args: list[str] | None = ["--disable-gpu"]
        if browser in _SYSTEM_BROWSERS:
            channel = browser.value
    elif engine is BrowserEngine.FIREFOX:
        browser_type = playwright.firefox
        args = None
    else:
        browser_type = playwright.webkit
        args = None
    try:
        return browser_type.launch(headless=headless, channel=channel, args=args)
    except Exception as e:
        reason = str(e)
        if _NOT_INSTALLED_PATTERN.search(reason) is None:
            raise
        first_line = reason.strip().splitlines()[0] if reason.strip() else ""
        raise BrowserNotInstalledError(
            f"{BROWSER_DISPLAY_NAMES[browser]} is not installed on this test "
            f"machine ({first_line}). {browser_install_hint(browser)}"
        ) from e


@dataclass
class DownloadRecord:
    filename: str
    path: str
    status: str
    size: int = 0
    error: str = ""
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class _ExplicitDownload:
    """The download browser_download is waiting for, claimed by _on_download."""

    save_path: str | None
    record: DownloadRecord | None = None


class BrowserSession:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._browser_name: BrowserName | None = None
        self._element_frames: dict[int, Frame] = {}
        self._downloads: list[DownloadRecord] = []
        self._explicit_download: _ExplicitDownload | None = None

    def ensure_page(self, browser: BrowserName) -> Page:
        if self._browser_name is not None and self._browser_name is not browser:
            # Switching browser drops the old one with its page state; the
            # Playwright driver stays up for the new launch.
            self._close_browser()

        if self._page is not None and not self._page.is_closed():
            return self._page

        if self._playwright is None:
            self._playwright = sync_playwright().start()

        if self._browser is not None and not self._browser.is_connected():
            # Crashed or closed by hand: its context is dead too.
            self._close_browser()

        if self._browser is None:
            self._browser = launch_browser(self._playwright, browser, headless=False)
            self._browser_name = browser

        if self._context is None:
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                accept_downloads=True,
            )

        self._page = self._context.new_page()
        self._page.on("download", self._on_download)
        self._element_frames = {}
        return self._page

    def set_element_frames(self, mapping: dict[int, Frame]) -> None:
        """Remember which frame each element index was stamped in."""
        self._element_frames = mapping

    def frame_for_index(self, element_index: int) -> Frame | None:
        return self._element_frames.get(element_index)

    def close(self) -> None:
        self._close_browser()
        self.clear_downloads()

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.debug("Failed to stop playwright", exc_info=True)
            self._playwright = None

    def _close_browser(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                logger.debug("Failed to close browser context", exc_info=True)
            self._context = None

        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                logger.debug("Failed to close browser", exc_info=True)
            self._browser = None

        self._browser_name = None
        self._page = None
        self._element_frames = {}

    def clear_downloads(self) -> None:
        self._downloads.clear()

    def snapshot_downloads(self) -> list[DownloadRecord]:
        return list(self._downloads)

    @contextmanager
    def explicit_download(self, save_path: str | None) -> Iterator[_ExplicitDownload]:
        """Have _on_download save the next download to save_path."""
        claim = _ExplicitDownload(save_path=save_path)
        self._explicit_download = claim
        try:
            yield claim
        finally:
            self._explicit_download = None

    def wait_for_downloads(
        self, timeout_s: float, records: list[DownloadRecord] | None = None
    ) -> None:
        """Drive the event loop until no download (of records, else all) is in
        progress or time runs out."""
        watched = self._downloads if records is None else records
        self._drive_until(
            lambda: all(r.status != "in_progress" for r in watched), timeout_s
        )

    def wait_for_claim(self, claim: _ExplicitDownload, timeout_s: float) -> None:
        """Drive the event loop until _on_download claims a download."""
        self._drive_until(lambda: claim.record is not None, timeout_s)

    def _drive_until(self, done: Callable[[], bool], timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while not done():
            remaining_ms = (deadline - time.monotonic()) * 1000
            if remaining_ms <= 0:
                return
            page = self._any_open_page()
            if page is None:
                # A closed context fails or abandons every pending save.
                return
            try:
                page.wait_for_timeout(min(_DOWNLOAD_POLL_MS, remaining_ms))
            except PlaywrightError:
                # The page closed mid-wait; report what we have.
                return

    def _any_open_page(self) -> Page | None:
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._context is None:
            return None
        for page in self._context.pages:
            if not page.is_closed():
                return page
        return None

    def _on_download(self, download: Download) -> None:
        # In the sync API each protocol event is dispatched in its own greenlet on
        # the Playwright thread, so the blocking save_as below is supported: it
        # parks that greenlet and progresses while another Playwright call drives
        # the event loop. Later "download" listeners wait until it returns. It
        # must never move to another thread: the sync API is bound to this one.
        record = DownloadRecord(
            filename=download.suggested_filename,
            path="",
            status="in_progress",
        )
        self._downloads.append(record)
        save_path = Path.home() / "Downloads" / record.filename
        claim = self._explicit_download
        if claim is not None and claim.record is None:
            claim.record = record
            if claim.save_path:
                save_path = Path(claim.save_path)
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            download.save_as(str(save_path))
            record.size = save_path.stat().st_size
            record.path = str(save_path)
            record.status = "completed"
        except Exception as e:
            record.status = "failed"
            record.error = str(e)
            logger.warning("Download save failed for %s: %s", record.filename, e)


def execute_browser_navigate(
    session: BrowserSession, payload: BrowserNavigatePayload
) -> ActionResultPayload:
    start = time.monotonic()
    try:
        page = session.ensure_page(payload.browser)
        page.goto(payload.url, wait_until="domcontentloaded")
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser navigate failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Navigated to {payload.url}",
        duration_ms=duration_ms,
    )


def _element_frame(
    session: BrowserSession, browser: BrowserName, element_index: int
) -> Frame:
    """Return the frame an element index was stamped in.

    Indices are handed out across every frame on the page, so an index from an
    ad or consent iframe cannot be acted on through the main frame. Falling
    back to the main frame keeps an index from before the last collection
    behaving as it always did.
    """
    frame = session.frame_for_index(element_index)
    if frame is not None and not frame.is_detached():
        return frame
    return session.ensure_page(browser).main_frame


def _element_selector(element_index: int) -> str:
    return f'[data-at-idx="{element_index}"]'


def _locate_element(
    session: BrowserSession, browser: BrowserName, element_index: int
) -> Locator:
    """Resolve an element index, failing at once when it is not on the page.

    Playwright would otherwise wait out its 30 s default for the selector to
    appear, which is as long as the server waits for our reply, so the agent
    saw a bare timeout instead of learning the element was gone.
    """
    locator = _element_frame(session, browser, element_index).locator(
        _element_selector(element_index)
    )
    if locator.count() == 0:
        raise BrowserElementNotFoundError(
            f"Element [{element_index}] was not found on the page; the page "
            "may have changed since the elements were listed"
        )
    return locator


def _not_actionable_message(action: str, element_index: int) -> str:
    return (
        f"Element [{element_index}] is on the page but could not be {action} "
        f"within {_ACTION_TIMEOUT_MS // 1000}s; it may be hidden, disabled, "
        "or covered by another element"
    )


def execute_browser_click(
    session: BrowserSession, payload: BrowserClickPayload
) -> ActionResultPayload:
    start = time.monotonic()
    try:
        _locate_element(session, payload.browser, payload.element_index).click(
            timeout=_ACTION_TIMEOUT_MS
        )
    except ExecutionError:
        raise
    except PlaywrightTimeoutError as e:
        raise ExecutionError(
            _not_actionable_message("clicked", payload.element_index)
        ) from e
    except Exception as e:
        raise ExecutionError(f"Browser click failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Clicked element at index {payload.element_index}",
        duration_ms=duration_ms,
    )


def execute_browser_double_click(
    session: BrowserSession, payload: BrowserClickPayload
) -> ActionResultPayload:
    start = time.monotonic()
    try:
        _locate_element(session, payload.browser, payload.element_index).dblclick(
            timeout=_ACTION_TIMEOUT_MS
        )
    except ExecutionError:
        raise
    except PlaywrightTimeoutError as e:
        raise ExecutionError(
            _not_actionable_message("double-clicked", payload.element_index)
        ) from e
    except Exception as e:
        raise ExecutionError(f"Browser double-click failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Double-clicked element at index {payload.element_index}",
        duration_ms=duration_ms,
    )


def execute_browser_type(
    session: BrowserSession, payload: BrowserTypePayload
) -> ActionResultPayload:
    start = time.monotonic()
    try:
        _locate_element(session, payload.browser, payload.element_index).fill(
            payload.text, timeout=_ACTION_TIMEOUT_MS
        )
    except ExecutionError:
        raise
    except PlaywrightTimeoutError as e:
        raise ExecutionError(
            _not_actionable_message("typed into", payload.element_index)
        ) from e
    except Exception as e:
        raise ExecutionError(f"Browser type failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Typed into element at index {payload.element_index}",
        duration_ms=duration_ms,
    )


def execute_browser_hover(
    session: BrowserSession, payload: BrowserHoverPayload
) -> ActionResultPayload:
    start = time.monotonic()
    try:
        _locate_element(session, payload.browser, payload.element_index).hover(
            timeout=_ACTION_TIMEOUT_MS
        )
    except ExecutionError:
        raise
    except PlaywrightTimeoutError as e:
        raise ExecutionError(
            _not_actionable_message("hovered", payload.element_index)
        ) from e
    except Exception as e:
        raise ExecutionError(f"Browser hover failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Hovered element at index {payload.element_index}",
        duration_ms=duration_ms,
    )


def _collect_frame_elements(frame: Frame, start_index: int) -> list[object]:
    try:
        raw_elements = frame.evaluate(_COLLECT_ELEMENTS_JS, start_index)
    except Exception:
        # Frames detach, navigate or die mid-collection, especially ad frames.
        # One unreachable frame must not cost us the rest of the page.
        logger.debug(
            "Skipped element collection for frame %s", frame.url, exc_info=True
        )
        return []
    if not isinstance(raw_elements, list):
        return []
    return raw_elements


def _frame_label(frame: Frame, main_frame: Frame) -> str:
    if frame is main_frame:
        return ""
    host = urlparse(frame.url).netloc
    return f"iframe:{host}" if host else "iframe"


def execute_browser_get_elements(
    session: BrowserSession, payload: BrowserTargetPayload
) -> BrowserContentResultPayload:
    start = time.monotonic()
    try:
        page = session.ensure_page(payload.browser)
        collected: list[object] = []
        index_frames: dict[int, Frame] = {}

        for frame in page.frames:
            elements = _collect_frame_elements(frame, len(collected))
            label = _frame_label(frame, page.main_frame)
            for element in elements:
                if not isinstance(element, dict):
                    continue
                index = element.get("idx")
                if isinstance(index, int):
                    index_frames[index] = frame
                if label:
                    element["frame"] = label
            collected.extend(elements)

        session.set_element_frames(index_frames)
        content = _build_element_list(collected)
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser get elements failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return BrowserContentResultPayload(
        success=True,
        content=content,
        duration_ms=duration_ms,
    )


def execute_browser_get_page_content(
    session: BrowserSession, payload: BrowserTargetPayload
) -> BrowserContentResultPayload:
    start = time.monotonic()
    try:
        page = session.ensure_page(payload.browser)
        content = page.inner_text("body")
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser get page content failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return BrowserContentResultPayload(
        success=True,
        content=content,
        duration_ms=duration_ms,
    )


def execute_browser_get_url(
    session: BrowserSession, payload: BrowserTargetPayload
) -> BrowserContentResultPayload:
    start = time.monotonic()
    try:
        page = session.ensure_page(payload.browser)
        content = page.url
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser get URL failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return BrowserContentResultPayload(
        success=True,
        content=content,
        duration_ms=duration_ms,
    )


def execute_browser_take_screenshot(
    session: BrowserSession, payload: BrowserTargetPayload
) -> ScreenshotResponsePayload:
    start = time.monotonic()
    try:
        page = session.ensure_page(payload.browser)
        screenshot_bytes = page.screenshot(type="png")
        image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        viewport = page.viewport_size
        width = viewport["width"] if viewport else 1280
        height = viewport["height"] if viewport else 720
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser screenshot failed: {e}") from e
    return ScreenshotResponsePayload(
        success=True,
        image_base64=image_base64,
        width=width,
        height=height,
        format="png",
    )


def execute_browser_download(
    session: BrowserSession, payload: BrowserDownloadPayload
) -> ActionResultPayload:
    start = time.monotonic()
    # Kept out of the download try: a launch timeout must not reach
    # _handle_download_timeout, which needs the page.
    try:
        page = session.ensure_page(payload.browser)
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser download failed: {e}") from e
    # _on_download saves the file in its own greenlet; this only waits for it,
    # bounded, so a stalled download cannot wedge the Playwright thread. No
    # expect_download: its listener runs after _on_download in the same
    # greenlet, so it would only fire once the save had finished.
    try:
        goto_error: PlaywrightError | None = None
        with session.explicit_download(payload.save_path) as claim:
            try:
                page.goto(payload.url)
            except PlaywrightError as e:
                # A URL that downloads aborts the navigation even though the
                # download starts; only fatal if no download is captured.
                goto_error = e
            session.wait_for_claim(claim, _DOWNLOAD_TIMEOUT_MS / 1000)
        record = claim.record
        if record is None:
            if goto_error is not None:
                raise ExecutionError(
                    f"Browser download failed: {goto_error}"
                ) from goto_error
            _handle_download_timeout(page)
        remaining_s = _EXPLICIT_DOWNLOAD_BUDGET_S - (time.monotonic() - start)
        session.wait_for_downloads(max(0.0, remaining_s), [record])
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser download failed: {e}") from e
    if record.status == "failed":
        raise ExecutionError(f"Browser download failed: {record.error}")
    if record.status == "in_progress":
        raise ExecutionError(
            f"Browser download failed: {record.filename} still in progress after "
            f"{_EXPLICIT_DOWNLOAD_BUDGET_S:.0f}s; check browser_list_downloads later"
        )
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Downloaded to {record.path} ({record.size} bytes)",
        duration_ms=duration_ms,
    )


def execute_browser_list_downloads(
    session: BrowserSession,
) -> ActionResultPayload:
    start = time.monotonic()
    snapshot = session.snapshot_downloads()
    if not snapshot:
        duration_ms = (time.monotonic() - start) * 1000
        return ActionResultPayload(
            success=True,
            message="No downloads recorded.",
            duration_ms=duration_ms,
        )
    session.wait_for_downloads(_LIST_DOWNLOADS_WAIT_S)
    message = _format_downloads_list(session.snapshot_downloads())
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=message,
        duration_ms=duration_ms,
    )


def _format_downloads_list(records: list[DownloadRecord]) -> str:
    lines: list[str] = []
    for i, r in enumerate(records, 1):
        parts = [f"[{i}] {r.filename} - {r.status}"]
        if r.path:
            parts.append(f"  Path: {r.path}")
        if r.size > 0:
            parts.append(f"  Size: {_format_size(r.size)}")
        if r.error:
            parts.append(f"  Error: {r.error}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _build_element_list(elements: list[object]) -> str:
    lines: list[str] = []
    for item in elements:
        if not isinstance(item, dict):
            continue
        idx = item.get("idx", "?")
        tag = item.get("tag", "unknown")
        text = item.get("text", "")
        role = item.get("role", "")
        aria_label = item.get("ariaLabel", "")
        placeholder = item.get("placeholder", "")
        el_type = item.get("type", "")
        name = item.get("name", "")
        el_id = item.get("id", "")
        href = item.get("href", "")
        frame_label = item.get("frame", "")

        parts = [f"[{idx}] <{tag}>"]
        if text:
            parts.append(f'text="{text}"')
        if role:
            parts.append(f'role="{role}"')
        if aria_label:
            parts.append(f'aria-label="{aria_label}"')
        if placeholder:
            parts.append(f'placeholder="{placeholder}"')
        if el_type:
            parts.append(f'type="{el_type}"')
        if name:
            parts.append(f'name="{name}"')
        if el_id:
            parts.append(f'id="{el_id}"')
        if href:
            parts.append(f'href="{href}"')
        if frame_label:
            parts.append(f'frame="{frame_label}"')

        lines.append(" ".join(parts))
    return "\n".join(lines)
