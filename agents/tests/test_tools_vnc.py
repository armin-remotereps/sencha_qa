from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.tools_vnc import (
    vnc_click,
    vnc_hover,
    vnc_key_press,
    vnc_take_screenshot,
    vnc_type,
)
from agents.services.vnc_element_finder import VncElementNotFoundError
from agents.types import DMRConfig


@pytest.fixture
def mock_vnc_session() -> MagicMock:
    mock = MagicMock()
    mock.capture_screen.return_value = b"\x89PNG\r\n\x1a\nfake"
    return mock


@pytest.fixture
def vision_config() -> DMRConfig:
    return DMRConfig(
        host="test", port="8080", model="test-vision", temperature=0.0, max_tokens=1000
    )


# ============================================================================
# vnc_take_screenshot
# ============================================================================


@patch("agents.services.tools_vnc.answer_screenshot_question")
def test_vnc_take_screenshot_success(
    mock_answer: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    """vnc_take_screenshot captures and answers question."""
    mock_answer.return_value = "The desktop shows a terminal window."

    result = vnc_take_screenshot(
        mock_vnc_session, question="What do you see?", vision_config=vision_config
    )

    assert result.is_error is False
    assert "terminal window" in result.content
    mock_vnc_session.capture_screen.assert_called_once()
    mock_answer.assert_called_once()


@patch("agents.services.tools_vnc.answer_screenshot_question")
def test_vnc_take_screenshot_capture_failure(
    mock_answer: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    """vnc_take_screenshot returns error if capture fails."""
    mock_vnc_session.capture_screen.side_effect = RuntimeError("VNC down")

    result = vnc_take_screenshot(
        mock_vnc_session, question="What?", vision_config=vision_config
    )

    assert result.is_error is True
    assert "VNC down" in result.content


# ============================================================================
# vnc_click
# ============================================================================


@patch("agents.services.tools_vnc.find_element_coordinates")
def test_vnc_click_success(
    mock_find: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    """vnc_click finds element and clicks it."""
    mock_find.return_value = (250, 180)

    result = vnc_click(
        mock_vnc_session, description="OK button", vision_config=vision_config
    )

    assert result.is_error is False
    assert "250" in result.content
    assert "180" in result.content
    assert "OK button" in result.content
    mock_vnc_session.mouse_click.assert_called_once_with(250, 180)


@patch("agents.services.tools_vnc.find_element_coordinates")
def test_vnc_click_element_not_found(
    mock_find: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    """vnc_click returns error when element not found."""
    mock_find.side_effect = VncElementNotFoundError("Element not found on screen: X")

    result = vnc_click(
        mock_vnc_session, description="invisible", vision_config=vision_config
    )

    assert result.is_error is True
    assert "not found" in result.content


# ============================================================================
# vnc_type
# ============================================================================


@patch("agents.services.tools_vnc.find_element_coordinates")
def test_vnc_type_success(
    mock_find: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    """vnc_type finds input, clicks to focus, then types text."""
    mock_find.return_value = (300, 250)

    result = vnc_type(
        mock_vnc_session,
        description="username field",
        text="admin",
        vision_config=vision_config,
    )

    assert result.is_error is False
    assert "admin" in result.content
    assert "username field" in result.content
    mock_vnc_session.mouse_click.assert_called_once_with(300, 250)
    mock_vnc_session.type_text.assert_called_once_with("admin")


@patch("agents.services.tools_vnc.find_element_coordinates")
def test_vnc_type_element_not_found(
    mock_find: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    """vnc_type returns error when target element not found."""
    mock_find.side_effect = VncElementNotFoundError("not found")

    result = vnc_type(
        mock_vnc_session,
        description="missing field",
        text="data",
        vision_config=vision_config,
    )

    assert result.is_error is True
    assert "not found" in result.content


# ============================================================================
# vnc_hover
# ============================================================================


@patch("agents.services.tools_vnc.find_element_coordinates")
def test_vnc_hover_success(
    mock_find: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    """vnc_hover finds element and moves mouse to it."""
    mock_find.return_value = (500, 100)

    result = vnc_hover(
        mock_vnc_session, description="settings icon", vision_config=vision_config
    )

    assert result.is_error is False
    assert "500" in result.content
    assert "100" in result.content
    mock_vnc_session.mouse_move.assert_called_once_with(500, 100)


# ============================================================================
# vnc_key_press
# ============================================================================


def test_vnc_key_press_success(mock_vnc_session: MagicMock) -> None:
    """vnc_key_press sends key via VNC."""
    result = vnc_key_press(mock_vnc_session, keys="Return")

    assert result.is_error is False
    assert "Return" in result.content
    mock_vnc_session.key_press.assert_called_once_with("Return")


def test_vnc_key_press_failure(mock_vnc_session: MagicMock) -> None:
    """vnc_key_press returns error on failure."""
    mock_vnc_session.key_press.side_effect = RuntimeError("VNC disconnected")

    result = vnc_key_press(mock_vnc_session, keys="ctrl-a")

    assert result.is_error is True
    assert "VNC disconnected" in result.content
