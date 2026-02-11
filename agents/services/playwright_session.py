from __future__ import annotations

import logging
import threading
from types import TracebackType

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from environments.services import get_playwright_cdp_url
from environments.types import ContainerPorts

logger = logging.getLogger(__name__)

BROWSER_TIMEOUT = 30000  # 30 seconds


class PlaywrightSessionManager:
    """Persistent Playwright CDP connection manager for an agent run.

    Lazily connects on first ``get_page()`` call.  Auto-reconnects once if
    the browser connection is dead.

    Usage::

        with PlaywrightSessionManager(ports) as pw:
            page = pw.get_page()
            page.goto("https://example.com")
    """

    def __init__(self, ports: ContainerPorts) -> None:
        self._ports = ports
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> PlaywrightSessionManager:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._is_browser_connected()

    def get_page(self) -> Page:
        """Return the persistent page, connecting lazily if needed."""
        with self._lock:
            return self._get_page_locked()

    def connect(self) -> None:
        """Open the CDP connection (idempotent if already connected)."""
        with self._lock:
            if self._is_browser_connected():
                return
            self._connect_locked()

    def close(self) -> None:
        """Close the Playwright connection if open."""
        with self._lock:
            self._close_locked()

    # ------------------------------------------------------------------
    # Private helpers (must be called under self._lock)
    # ------------------------------------------------------------------

    def _is_browser_connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    def _connect_locked(self) -> None:
        self._close_locked()
        pw = sync_playwright().start()
        cdp_url = get_playwright_cdp_url(self._ports)
        browser = pw.chromium.connect_over_cdp(cdp_url)

        # Get or create a page
        contexts = browser.contexts
        if contexts and contexts[0].pages:
            page = contexts[0].pages[0]
        elif contexts:
            page = contexts[0].new_page()
        else:
            page = browser.new_page()

        self._playwright = pw
        self._browser = browser
        self._page = page
        logger.debug(
            "Playwright session connected to CDP port %d",
            self._ports.playwright_cdp,
        )

    def _close_locked(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                logger.debug("Error closing browser", exc_info=True)
            self._browser = None
            self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.debug("Error stopping playwright", exc_info=True)
            self._playwright = None

    def _get_page_locked(self) -> Page:
        """Get page with one automatic reconnection attempt."""
        if not self._is_browser_connected():
            self._connect_locked()

        if self._page is not None:
            try:
                # Quick check - accessing url should fail if page is closed
                _ = self._page.url
                return self._page
            except Exception:
                logger.warning("Page access failed, reconnecting")
                self._connect_locked()

        if self._page is None:
            msg = "Failed to establish Playwright page"
            raise RuntimeError(msg)
        return self._page
