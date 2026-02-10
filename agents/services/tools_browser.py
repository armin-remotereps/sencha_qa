from __future__ import annotations

import base64
import logging
from collections.abc import Generator
from contextlib import contextmanager

from playwright.sync_api import Page, sync_playwright

from agents.types import ToolResult
from environments.services import get_playwright_cdp_url
from environments.types import ContainerPorts

logger = logging.getLogger(__name__)

BROWSER_TIMEOUT = 30000  # 30 seconds


@contextmanager
def _browser_page(ports: ContainerPorts) -> Generator[Page, None, None]:
    """Connect to the container's browser via CDP and yield a page."""
    cdp_url = get_playwright_cdp_url(ports)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        try:
            contexts = browser.contexts
            if not contexts:
                page = browser.new_page()
            else:
                pages = contexts[0].pages
                page = pages[0] if pages else contexts[0].new_page()
            yield page
        finally:
            browser.close()


def browser_navigate(ports: ContainerPorts, *, url: str) -> ToolResult:
    """Navigate to a URL in the browser."""
    try:
        with _browser_page(ports) as page:
            page.goto(url, timeout=BROWSER_TIMEOUT)
            title = page.title()
        return ToolResult(
            tool_call_id="",
            content=f"Navigated to {url}. Page title: {title}",
            is_error=False,
        )
    except Exception as e:
        logger.error("Browser navigate failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Navigation error: {e}",
            is_error=True,
        )


def browser_click(ports: ContainerPorts, *, selector: str) -> ToolResult:
    """Click an element in the browser by CSS selector."""
    try:
        with _browser_page(ports) as page:
            page.click(selector, timeout=BROWSER_TIMEOUT)
        return ToolResult(
            tool_call_id="",
            content=f"Clicked element: {selector}",
            is_error=False,
        )
    except Exception as e:
        logger.error("Browser click failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Click error: {e}",
            is_error=True,
        )


def browser_type(ports: ContainerPorts, *, selector: str, text: str) -> ToolResult:
    """Type text into an element in the browser."""
    try:
        with _browser_page(ports) as page:
            page.fill(selector, text, timeout=BROWSER_TIMEOUT)
        return ToolResult(
            tool_call_id="",
            content=f"Typed '{text}' into {selector}",
            is_error=False,
        )
    except Exception as e:
        logger.error("Browser type failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Type error: {e}",
            is_error=True,
        )


def browser_get_page_content(
    ports: ContainerPorts, *, max_length: int = 5000
) -> ToolResult:
    """Get the text content of the current page."""
    try:
        with _browser_page(ports) as page:
            content = page.inner_text("body", timeout=BROWSER_TIMEOUT)
        if len(content) > max_length:
            content = content[:max_length] + "\n... (truncated)"
        return ToolResult(
            tool_call_id="",
            content=content,
            is_error=False,
        )
    except Exception as e:
        logger.error("Browser get content failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Get content error: {e}",
            is_error=True,
        )


def browser_get_url(ports: ContainerPorts) -> ToolResult:
    """Get the current URL of the browser."""
    try:
        with _browser_page(ports) as page:
            url = page.url
        return ToolResult(
            tool_call_id="",
            content=url,
            is_error=False,
        )
    except Exception as e:
        logger.error("Browser get URL failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Get URL error: {e}",
            is_error=True,
        )


def browser_take_screenshot(ports: ContainerPorts) -> ToolResult:
    """Take a screenshot of the browser viewport and return as base64."""
    try:
        with _browser_page(ports) as page:
            screenshot_bytes = page.screenshot()
        image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        return ToolResult(
            tool_call_id="",
            content="Browser screenshot captured.",
            is_error=False,
            image_base64=image_base64,
        )
    except Exception as e:
        logger.error("Browser screenshot failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"Screenshot error: {e}",
            is_error=True,
        )
