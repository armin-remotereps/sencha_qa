from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.element_finder import (
    AmbiguousElementError,
    ElementNotFoundError,
)
from agents.services.playwright_session import PlaywrightSessionManager
from agents.services.tools_browser import (
    browser_click,
    browser_get_page_content,
    browser_get_url,
    browser_hover,
    browser_navigate,
    browser_take_screenshot,
    browser_type,
)
from agents.types import DMRConfig


@pytest.fixture
def pw_session() -> MagicMock:
    """Fixture providing a mocked PlaywrightSessionManager."""
    return MagicMock(spec=PlaywrightSessionManager)


@pytest.fixture
def mock_page(pw_session: MagicMock) -> MagicMock:
    """Fixture providing a mock page returned by pw_session.get_page()."""
    page = MagicMock()
    page.title.return_value = "Test Page"
    page.url = "https://example.com"
    page.inner_text.return_value = "Test content"
    page.screenshot.return_value = b"fake_screenshot_data"
    pw_session.get_page.return_value = page
    return page


@pytest.fixture
def dmr_config() -> DMRConfig:
    """Fixture providing a DMRConfig for element finding."""
    return DMRConfig(host="localhost", port="8080", model="test-model")


# ============================================================================
# browser_navigate tests
# ============================================================================


def test_browser_navigate_success(pw_session: MagicMock, mock_page: MagicMock) -> None:
    """Test browser_navigate without vision_config returns title only."""
    result = browser_navigate(pw_session, url="https://example.com", vision_config=None)

    pw_session.get_page.assert_called_once()
    mock_page.goto.assert_called_once_with("https://example.com", timeout=30000)
    assert result.is_error is False
    assert "Navigated to https://example.com" in result.content
    assert "Test Page" in result.content
    assert "Page description" not in result.content


@patch("agents.services.tools_browser.answer_screenshot_question")
def test_browser_navigate_with_vision(
    mock_answer: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_navigate with vision_config includes page description."""
    mock_answer.return_value = "A login page with username and password fields."

    result = browser_navigate(
        pw_session, url="https://example.com", vision_config=dmr_config
    )

    pw_session.get_page.assert_called_once()
    mock_page.goto.assert_called_once_with("https://example.com", timeout=30000)
    mock_page.screenshot.assert_called_once()
    mock_answer.assert_called_once()
    assert result.is_error is False
    assert "Navigated to https://example.com" in result.content
    assert "Test Page" in result.content
    assert (
        "Page description: A login page with username and password fields."
        in result.content
    )


@patch("agents.services.tools_browser.answer_screenshot_question")
def test_browser_navigate_vision_failure_falls_back(
    mock_answer: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_navigate falls back to title-only when vision fails."""
    mock_answer.side_effect = Exception("Vision model unavailable")

    result = browser_navigate(
        pw_session, url="https://example.com", vision_config=dmr_config
    )

    assert result.is_error is False
    assert "Navigated to https://example.com" in result.content
    assert "Test Page" in result.content
    assert "Page description" not in result.content


def test_browser_navigate_exception(pw_session: MagicMock) -> None:
    """Test browser_navigate handles exceptions gracefully."""
    pw_session.get_page.side_effect = Exception("Connection failed")

    result = browser_navigate(pw_session, url="https://example.com")

    assert result.is_error is True
    assert "navigate error" in result.content
    assert "Connection failed" in result.content


# ============================================================================
# browser_click tests
# ============================================================================


@patch("agents.services.tools_browser.find_element_by_description")
def test_browser_click_success(
    mock_find: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_click finds element and clicks it."""
    mock_find.return_value = "#submit-button"

    result = browser_click(
        pw_session, description="the submit button", dmr_config=dmr_config
    )

    mock_find.assert_called_once_with(mock_page, "the submit button", dmr_config)
    mock_page.click.assert_called_once_with("#submit-button", timeout=30000)
    assert result.is_error is False
    assert "Clicked element: the submit button" in result.content


@patch("agents.services.tools_browser.find_element_by_description")
def test_browser_click_element_not_found(
    mock_find: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_click when element is not found."""
    mock_find.side_effect = ElementNotFoundError(
        "No element matches 'the submit button'"
    )

    result = browser_click(
        pw_session, description="the submit button", dmr_config=dmr_config
    )

    assert result.is_error is True
    assert "click error" in result.content
    assert "No element matches" in result.content


@patch("agents.services.tools_browser.find_element_by_description")
def test_browser_click_ambiguous(
    mock_find: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_click when multiple elements match."""
    mock_find.side_effect = AmbiguousElementError("Multiple elements match")

    result = browser_click(pw_session, description="a button", dmr_config=dmr_config)

    assert result.is_error is True
    assert "click error" in result.content
    assert "Multiple elements match" in result.content


# ============================================================================
# browser_type tests
# ============================================================================


@patch("agents.services.tools_browser.find_element_by_description")
def test_browser_type_success(
    mock_find: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_type finds element and types text into it."""
    mock_find.return_value = "#username"

    result = browser_type(
        pw_session,
        description="the username field",
        text="testuser",
        dmr_config=dmr_config,
    )

    mock_find.assert_called_once_with(mock_page, "the username field", dmr_config)
    mock_page.fill.assert_called_once_with("#username", "testuser", timeout=30000)
    assert result.is_error is False
    assert "Typed 'testuser' into element: the username field" in result.content


@patch("agents.services.tools_browser.find_element_by_description")
def test_browser_type_element_not_found(
    mock_find: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_type when element is not found."""
    mock_find.side_effect = ElementNotFoundError(
        "No element matches 'the username field'"
    )

    result = browser_type(
        pw_session,
        description="the username field",
        text="testuser",
        dmr_config=dmr_config,
    )

    assert result.is_error is True
    assert "type error" in result.content
    assert "No element matches" in result.content


# ============================================================================
# browser_hover tests
# ============================================================================


@patch("agents.services.tools_browser.find_element_by_description")
def test_browser_hover_success(
    mock_find: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_hover finds element and hovers over it."""
    mock_find.return_value = "#menu-item"

    result = browser_hover(
        pw_session, description="the menu item", dmr_config=dmr_config
    )

    mock_find.assert_called_once_with(mock_page, "the menu item", dmr_config)
    mock_page.hover.assert_called_once_with("#menu-item", timeout=30000)
    assert result.is_error is False
    assert "Hovered over element: the menu item" in result.content


@patch("agents.services.tools_browser.find_element_by_description")
def test_browser_hover_element_not_found(
    mock_find: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_hover when element is not found."""
    mock_find.side_effect = ElementNotFoundError("No element matches 'the menu item'")

    result = browser_hover(
        pw_session, description="the menu item", dmr_config=dmr_config
    )

    assert result.is_error is True
    assert "hover error" in result.content
    assert "No element matches" in result.content


# ============================================================================
# browser_get_page_content tests
# ============================================================================


def test_browser_get_page_content_success(
    pw_session: MagicMock, mock_page: MagicMock
) -> None:
    """Test browser_get_page_content returns page text content."""
    result = browser_get_page_content(pw_session, max_length=1000)

    pw_session.get_page.assert_called_once()
    mock_page.inner_text.assert_called_once_with("body", timeout=30000)
    assert result.is_error is False
    assert "Test content" in result.content


def test_browser_get_page_content_truncation(
    pw_session: MagicMock, mock_page: MagicMock
) -> None:
    """Test browser_get_page_content truncates long content."""
    mock_page.inner_text.return_value = "x" * 10000

    result = browser_get_page_content(pw_session, max_length=100)

    assert result.is_error is False
    assert len(result.content) <= 120  # 100 + "\n... (truncated)"
    assert "... (truncated)" in result.content


# ============================================================================
# browser_get_url tests
# ============================================================================


def test_browser_get_url_success(pw_session: MagicMock, mock_page: MagicMock) -> None:
    """Test browser_get_url returns the current URL."""
    result = browser_get_url(pw_session)

    pw_session.get_page.assert_called_once()
    assert result.is_error is False
    assert result.content == "https://example.com"


# ============================================================================
# browser_take_screenshot tests
# ============================================================================


@patch("agents.services.tools_browser.answer_screenshot_question")
def test_browser_take_screenshot_success(
    mock_answer: MagicMock,
    pw_session: MagicMock,
    mock_page: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    """Test browser_take_screenshot captures screenshot and answers question."""
    mock_answer.return_value = "I can see a login page with a form."

    result = browser_take_screenshot(
        pw_session,
        question="What is on the page?",
        vision_config=dmr_config,
    )

    pw_session.get_page.assert_called_once()
    mock_page.screenshot.assert_called_once()
    mock_answer.assert_called_once()
    assert result.is_error is False
    assert result.content == "I can see a login page with a form."
