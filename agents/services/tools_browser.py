from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from agents.services.element_finder import (
    AmbiguousElementError,
    ElementNotFoundError,
    find_element_by_description,
)
from agents.services.playwright_session import BROWSER_TIMEOUT, PlaywrightSessionManager
from agents.services.vision_qa import answer_screenshot_question
from agents.types import DMRConfig, ToolResult

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _safe_browser_call(
    operation: str,
    fn: Callable[[], ToolResult],
) -> ToolResult:
    """Execute *fn* and catch element-finder or generic errors."""
    try:
        return fn()
    except (ElementNotFoundError, AmbiguousElementError) as e:
        return ToolResult(
            tool_call_id="",
            content=f"{operation} error: {e}",
            is_error=True,
        )
    except Exception as e:
        logger.error("Browser %s failed: %s", operation, e)
        return ToolResult(
            tool_call_id="",
            content=f"{operation} error: {e}",
            is_error=True,
        )


def browser_navigate(
    pw_session: PlaywrightSessionManager,
    *,
    url: str,
    vision_config: DMRConfig | None = None,
) -> ToolResult:
    """Navigate to a URL in the browser."""

    def _do() -> ToolResult:
        page = pw_session.get_page()
        page.goto(url, timeout=BROWSER_TIMEOUT)
        title = page.title()
        content = f"Navigated to {url}. Page title: {title}"

        if vision_config is not None:
            try:
                screenshot_bytes = page.screenshot()
                image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                description = answer_screenshot_question(
                    vision_config, image_base64, "Describe what this page is showing."
                )
                content += f"\n\nPage description: {description}"
            except Exception as e:
                logger.warning("Vision description failed after navigate: %s", e)

        return ToolResult(
            tool_call_id="",
            content=content,
            is_error=False,
        )

    return _safe_browser_call("navigate", _do)


def browser_click(
    pw_session: PlaywrightSessionManager,
    *,
    description: str,
    dmr_config: DMRConfig,
) -> ToolResult:
    """Click an element by natural-language description."""

    def _do() -> ToolResult:
        page = pw_session.get_page()
        selector = find_element_by_description(page, description, dmr_config)
        page.click(selector, timeout=BROWSER_TIMEOUT)
        return ToolResult(
            tool_call_id="",
            content=f"Clicked element: {description}",
            is_error=False,
        )

    return _safe_browser_call("click", _do)


def browser_type(
    pw_session: PlaywrightSessionManager,
    *,
    description: str,
    text: str,
    dmr_config: DMRConfig,
) -> ToolResult:
    """Type text into an element found by natural-language description."""

    def _do() -> ToolResult:
        page = pw_session.get_page()
        selector = find_element_by_description(page, description, dmr_config)
        page.fill(selector, text, timeout=BROWSER_TIMEOUT)
        return ToolResult(
            tool_call_id="",
            content=f"Typed '{text}' into element: {description}",
            is_error=False,
        )

    return _safe_browser_call("type", _do)


def browser_hover(
    pw_session: PlaywrightSessionManager,
    *,
    description: str,
    dmr_config: DMRConfig,
) -> ToolResult:
    """Hover over an element found by natural-language description."""

    def _do() -> ToolResult:
        page = pw_session.get_page()
        selector = find_element_by_description(page, description, dmr_config)
        page.hover(selector, timeout=BROWSER_TIMEOUT)
        return ToolResult(
            tool_call_id="",
            content=f"Hovered over element: {description}",
            is_error=False,
        )

    return _safe_browser_call("hover", _do)


def browser_get_page_content(
    pw_session: PlaywrightSessionManager, *, max_length: int = 5000
) -> ToolResult:
    """Get the text content of the current page."""

    def _do() -> ToolResult:
        page = pw_session.get_page()
        content = page.inner_text("body", timeout=BROWSER_TIMEOUT)
        if len(content) > max_length:
            content = content[:max_length] + "\n... (truncated)"
        return ToolResult(
            tool_call_id="",
            content=content,
            is_error=False,
        )

    return _safe_browser_call("get_page_content", _do)


def browser_get_url(pw_session: PlaywrightSessionManager) -> ToolResult:
    """Get the current URL of the browser."""

    def _do() -> ToolResult:
        page = pw_session.get_page()
        return ToolResult(
            tool_call_id="",
            content=page.url,
            is_error=False,
        )

    return _safe_browser_call("get_url", _do)


def browser_take_screenshot(
    pw_session: PlaywrightSessionManager,
    *,
    question: str,
    vision_config: DMRConfig,
) -> ToolResult:
    """Take a browser screenshot and answer a question about it."""

    def _do() -> ToolResult:
        page = pw_session.get_page()
        screenshot_bytes = page.screenshot()
        image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        answer = answer_screenshot_question(vision_config, image_base64, question)
        return ToolResult(
            tool_call_id="",
            content=answer,
            is_error=False,
        )

    return _safe_browser_call("screenshot", _do)
