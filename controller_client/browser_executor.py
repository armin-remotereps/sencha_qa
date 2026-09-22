import base64
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Download,
    Frame,
    Page,
    Playwright,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import (
    sync_playwright,
)

from controller_client.exceptions import ExecutionError
from controller_client.protocol import (
    ActionResultPayload,
    BrowserClickPayload,
    BrowserContentResultPayload,
    BrowserDownloadPayload,
    BrowserHoverPayload,
    BrowserNavigatePayload,
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


def _handle_download_timeout(page: Page) -> None:
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
    completed_event: threading.Event = field(default_factory=threading.Event)


class BrowserSession:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._element_frames: dict[int, Frame] = {}
        self._downloads: list[DownloadRecord] = []
        self._downloads_lock: threading.Lock = threading.Lock()

    def ensure_page(self) -> Page:
        if self._page is not None and not self._page.is_closed():
            return self._page

        if self._playwright is None:
            self._playwright = sync_playwright().start()

        if self._browser is None or not self._browser.is_connected():
            self._browser = self._playwright.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-gpu"],
            )

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

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.debug("Failed to stop playwright", exc_info=True)
            self._playwright = None

        self._page = None
        self._element_frames = {}
        self.clear_downloads()

    def clear_downloads(self) -> None:
        with self._downloads_lock:
            self._downloads.clear()

    def snapshot_downloads(self) -> list[DownloadRecord]:
        with self._downloads_lock:
            return list(self._downloads)

    def _on_download(self, download: Download) -> None:
        record = DownloadRecord(
            filename=download.suggested_filename,
            path="",
            status="in_progress",
        )
        with self._downloads_lock:
            self._downloads.append(record)
        thread = threading.Thread(
            target=self._save_download,
            args=(download, record),
            daemon=True,
        )
        thread.start()

    def _save_download(self, download: Download, record: DownloadRecord) -> None:
        save_path = Path.home() / "Downloads" / record.filename
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            download.save_as(str(save_path))
            file_size = save_path.stat().st_size
            with self._downloads_lock:
                record.path = str(save_path)
                record.size = file_size
                record.status = "completed"
        except Exception as e:
            with self._downloads_lock:
                record.status = "failed"
                record.error = str(e)
            logger.warning("Download save failed for %s: %s", record.filename, e)
        record.completed_event.set()


def execute_browser_navigate(
    session: BrowserSession, payload: BrowserNavigatePayload
) -> ActionResultPayload:
    start = time.monotonic()
    try:
        page = session.ensure_page()
        page.goto(payload.url, wait_until="domcontentloaded")
    except Exception as e:
        raise ExecutionError(f"Browser navigate failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Navigated to {payload.url}",
        duration_ms=duration_ms,
    )


def _element_frame(session: BrowserSession, element_index: int) -> Frame:
    """Return the frame an element index was stamped in.

    Indices are handed out across every frame on the page, so an index from an
    ad or consent iframe cannot be acted on through the main frame. Falling
    back to the main frame keeps an index from before the last collection
    behaving as it always did.
    """
    frame = session.frame_for_index(element_index)
    if frame is not None and not frame.is_detached():
        return frame
    return session.ensure_page().main_frame


def _element_selector(element_index: int) -> str:
    return f'[data-at-idx="{element_index}"]'


def execute_browser_click(
    session: BrowserSession, payload: BrowserClickPayload
) -> ActionResultPayload:
    start = time.monotonic()
    try:
        frame = _element_frame(session, payload.element_index)
        frame.click(_element_selector(payload.element_index))
    except Exception as e:
        raise ExecutionError(f"Browser click failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Clicked element at index {payload.element_index}",
        duration_ms=duration_ms,
    )


def execute_browser_type(
    session: BrowserSession, payload: BrowserTypePayload
) -> ActionResultPayload:
    start = time.monotonic()
    try:
        frame = _element_frame(session, payload.element_index)
        frame.fill(_element_selector(payload.element_index), payload.text)
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
        frame = _element_frame(session, payload.element_index)
        frame.hover(_element_selector(payload.element_index))
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
    session: BrowserSession,
) -> BrowserContentResultPayload:
    start = time.monotonic()
    try:
        page = session.ensure_page()
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
    except Exception as e:
        raise ExecutionError(f"Browser get elements failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return BrowserContentResultPayload(
        success=True,
        content=content,
        duration_ms=duration_ms,
    )


def execute_browser_get_page_content(
    session: BrowserSession,
) -> BrowserContentResultPayload:
    start = time.monotonic()
    try:
        page = session.ensure_page()
        content = page.inner_text("body")
    except Exception as e:
        raise ExecutionError(f"Browser get page content failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return BrowserContentResultPayload(
        success=True,
        content=content,
        duration_ms=duration_ms,
    )


def execute_browser_get_url(
    session: BrowserSession,
) -> BrowserContentResultPayload:
    start = time.monotonic()
    try:
        page = session.ensure_page()
        content = page.url
    except Exception as e:
        raise ExecutionError(f"Browser get URL failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return BrowserContentResultPayload(
        success=True,
        content=content,
        duration_ms=duration_ms,
    )


def execute_browser_take_screenshot(
    session: BrowserSession,
) -> ScreenshotResponsePayload:
    start = time.monotonic()
    try:
        page = session.ensure_page()
        screenshot_bytes = page.screenshot(type="png")
        image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        viewport = page.viewport_size
        width = viewport["width"] if viewport else 1280
        height = viewport["height"] if viewport else 720
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
    try:
        page = session.ensure_page()
        with page.expect_download(timeout=_DOWNLOAD_TIMEOUT_MS) as download_info:
            page.goto(payload.url)
        download = download_info.value
        save_path = payload.save_path or str(
            Path.home() / "Downloads" / download.suggested_filename
        )
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        download.save_as(save_path)
        file_size = Path(save_path).stat().st_size
    except PlaywrightTimeoutError:
        _handle_download_timeout(page)
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser download failed: {e}") from e
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=True,
        message=f"Downloaded to {save_path} ({file_size} bytes)",
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
    deadline = time.monotonic() + _LIST_DOWNLOADS_WAIT_S
    for record in snapshot:
        if record.status == "in_progress":
            remaining = max(0.0, deadline - time.monotonic())
            record.completed_event.wait(timeout=remaining)
    message = _format_downloads_list(snapshot)
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
