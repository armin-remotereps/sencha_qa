from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.tools_browser import (
    browser_click,
    browser_get_page_content,
    browser_get_url,
    browser_navigate,
    browser_take_screenshot,
    browser_type,
)
from environments.types import ContainerPorts


@pytest.fixture
def test_ports() -> ContainerPorts:
    """Fixture for test container ports."""
    return ContainerPorts(ssh=2222, vnc=5901, playwright_cdp=9223)


def _create_mock_playwright(
    has_context: bool = True, has_page: bool = True
) -> MagicMock:
    """Create a mock Playwright instance with the full CDP connection chain.

    Args:
        has_context: Whether to include a browser context
        has_page: Whether to include a page in the context

    Returns:
        Mock sync_playwright context manager
    """
    mock_page = MagicMock()
    mock_page.title.return_value = "Test Page"
    mock_page.url = "https://example.com"
    mock_page.inner_text.return_value = "Test content"
    mock_page.screenshot.return_value = b"fake_screenshot_data"

    mock_context = MagicMock()
    mock_context.pages = [mock_page] if has_page else []

    mock_browser = MagicMock()
    mock_browser.contexts = [mock_context] if has_context else []
    mock_browser.new_page.return_value = mock_page

    mock_pw = MagicMock()
    mock_pw.chromium.connect_over_cdp.return_value = mock_browser

    mock_sync_pw = MagicMock()
    mock_sync_pw.__enter__ = MagicMock(return_value=mock_pw)
    mock_sync_pw.__exit__ = MagicMock(return_value=False)

    return mock_sync_pw


def test_browser_navigate_success(test_ports: ContainerPorts) -> None:
    """Test browser_navigate successfully navigates to a URL."""
    mock_sync_pw = _create_mock_playwright()

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_navigate(test_ports, url="https://example.com")

    assert result.is_error is False
    assert "Navigated to https://example.com" in result.content
    assert "Test Page" in result.content


def test_browser_navigate_exception(test_ports: ContainerPorts) -> None:
    """Test browser_navigate handles exceptions gracefully."""
    mock_sync_pw = MagicMock()
    mock_sync_pw.__enter__ = MagicMock(side_effect=Exception("Connection failed"))

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_navigate(test_ports, url="https://example.com")

    assert result.is_error is True
    assert "Navigation error" in result.content


def test_browser_click_success(test_ports: ContainerPorts) -> None:
    """Test browser_click successfully clicks an element."""
    mock_sync_pw = _create_mock_playwright()

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_click(test_ports, selector="#submit-button")

    assert result.is_error is False
    assert "Clicked element: #submit-button" in result.content


def test_browser_click_no_context_creates_page(test_ports: ContainerPorts) -> None:
    """Test browser_click creates a new page when no context exists."""
    mock_sync_pw = _create_mock_playwright(has_context=False)

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_click(test_ports, selector="#submit-button")

    assert result.is_error is False
    assert "Clicked element: #submit-button" in result.content


def test_browser_type_success(test_ports: ContainerPorts) -> None:
    """Test browser_type successfully types text into an element."""
    mock_sync_pw = _create_mock_playwright()

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_type(test_ports, selector="#username", text="testuser")

    assert result.is_error is False
    assert "Typed 'testuser' into #username" in result.content


def test_browser_get_page_content_success(test_ports: ContainerPorts) -> None:
    """Test browser_get_page_content returns page text content."""
    mock_sync_pw = _create_mock_playwright()

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_get_page_content(test_ports, max_length=1000)

    assert result.is_error is False
    assert "Test content" in result.content


def test_browser_get_page_content_truncation(test_ports: ContainerPorts) -> None:
    """Test browser_get_page_content truncates long content."""
    mock_sync_pw = _create_mock_playwright()
    mock_page = (
        mock_sync_pw.__enter__().chromium.connect_over_cdp().contexts[0].pages[0]
    )
    mock_page.inner_text.return_value = "x" * 10000

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_get_page_content(test_ports, max_length=100)

    assert result.is_error is False
    assert len(result.content) <= 120  # 100 + "... (truncated)"
    assert "... (truncated)" in result.content


def test_browser_get_url_success(test_ports: ContainerPorts) -> None:
    """Test browser_get_url returns the current URL."""
    mock_sync_pw = _create_mock_playwright()

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_get_url(test_ports)

    assert result.is_error is False
    assert result.content == "https://example.com"


def test_browser_take_screenshot_success(test_ports: ContainerPorts) -> None:
    """Test browser_take_screenshot returns base64 encoded screenshot."""
    mock_sync_pw = _create_mock_playwright()

    with patch(
        "agents.services.tools_browser.sync_playwright", return_value=mock_sync_pw
    ):
        result = browser_take_screenshot(test_ports)

    assert result.is_error is False
    assert "Browser screenshot captured" in result.content
    assert result.image_base64 is not None
    # Verify it's valid base64 encoded data
    assert len(result.image_base64) > 0
