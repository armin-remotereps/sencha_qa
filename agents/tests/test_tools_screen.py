from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.services.ssh_session import SSHSessionManager
from agents.services.tools_screen import (
    screen_click,
    screen_get_active_window,
    screen_key_press,
    screen_list_windows,
    screen_type_text,
    take_screenshot,
)
from environments.types import SSHResult


@pytest.fixture
def mock_ssh_session() -> MagicMock:
    """Fixture providing a mocked SSHSessionManager."""
    return MagicMock(spec=SSHSessionManager)


# ============================================================================
# take_screenshot tests
# ============================================================================


def test_take_screenshot_happy_path(mock_ssh_session: MagicMock) -> None:
    """Test take_screenshot successfully captures and encodes screenshot."""
    capture_result = SSHResult(exit_code=0, stdout="", stderr="")
    base64_result = SSHResult(
        exit_code=0,
        stdout="iVBORw0KGgoAAAANSUhEUgAAAAUA\nAAAFCAYAAACNbyblAAAAHElEQVQI12P4\n",
        stderr="",
    )
    mock_ssh_session.execute.side_effect = [capture_result, base64_result]

    result = take_screenshot(mock_ssh_session)

    assert mock_ssh_session.execute.call_count == 2
    first_call = mock_ssh_session.execute.call_args_list[0]
    assert "scrot -o /tmp/screenshot.png" in first_call[0][0]
    second_call = mock_ssh_session.execute.call_args_list[1]
    assert "base64 -w 0 /tmp/screenshot.png" in second_call[0][0]

    assert result.is_error is False
    assert result.content == "Screenshot captured successfully."
    assert (
        result.image_base64
        == "iVBORw0KGgoAAAANSUhEUgAAAAUA\nAAAFCAYAAACNbyblAAAAHElEQVQI12P4".strip()
    )


def test_take_screenshot_capture_fails(mock_ssh_session: MagicMock) -> None:
    """Test take_screenshot when scrot capture command fails."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=1,
        stdout="",
        stderr="scrot: Can't open X display",
    )

    result = take_screenshot(mock_ssh_session)

    mock_ssh_session.execute.assert_called_once()
    assert result.is_error is True
    assert "Screenshot capture failed:" in result.content
    assert "Can't open X display" in result.content
    assert result.image_base64 is None


def test_take_screenshot_base64_read_fails(mock_ssh_session: MagicMock) -> None:
    """Test take_screenshot when base64 read command fails."""
    capture_result = SSHResult(exit_code=0, stdout="", stderr="")
    base64_result = SSHResult(
        exit_code=1,
        stdout="",
        stderr="base64: /tmp/screenshot.png: No such file or directory",
    )
    mock_ssh_session.execute.side_effect = [capture_result, base64_result]

    result = take_screenshot(mock_ssh_session)

    assert result.is_error is True
    assert "Screenshot read failed:" in result.content
    assert "No such file or directory" in result.content
    assert result.image_base64 is None


def test_take_screenshot_ssh_exception(mock_ssh_session: MagicMock) -> None:
    """Test take_screenshot when SSH session raises exception."""
    mock_ssh_session.execute.side_effect = Exception("Connection failed")

    result = take_screenshot(mock_ssh_session)

    assert result.is_error is True
    assert "Screenshot error:" in result.content
    assert "Connection failed" in result.content
    assert result.image_base64 is None


# ============================================================================
# screen_click tests
# ============================================================================


def test_screen_click_happy_path(mock_ssh_session: MagicMock) -> None:
    """Test screen_click successfully clicks at coordinates."""
    mock_ssh_session.execute.return_value = SSHResult(exit_code=0, stdout="", stderr="")

    result = screen_click(mock_ssh_session, x=100, y=200, button=1)

    mock_ssh_session.execute.assert_called_once()
    cmd_arg = mock_ssh_session.execute.call_args[0][0]
    assert "xdotool mousemove 100 200 click 1" in cmd_arg
    assert "DISPLAY=:0" in cmd_arg

    assert result.is_error is False
    assert result.content == "Clicked at (100, 200) with button 1."


def test_screen_click_default_button(mock_ssh_session: MagicMock) -> None:
    """Test screen_click uses default button=1 when not specified."""
    mock_ssh_session.execute.return_value = SSHResult(exit_code=0, stdout="", stderr="")

    result = screen_click(mock_ssh_session, x=50, y=75)

    cmd_arg = mock_ssh_session.execute.call_args[0][0]
    assert "click 1" in cmd_arg
    assert result.content == "Clicked at (50, 75) with button 1."


def test_screen_click_failure(mock_ssh_session: MagicMock) -> None:
    """Test screen_click when xdotool command fails."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=1,
        stdout="",
        stderr="Error: Can't open display",
    )

    result = screen_click(mock_ssh_session, x=10, y=20)

    assert result.is_error is True
    assert "Click failed:" in result.content
    assert "Can't open display" in result.content


def test_screen_click_ssh_exception(mock_ssh_session: MagicMock) -> None:
    """Test screen_click when SSH raises exception."""
    mock_ssh_session.execute.side_effect = Exception("SSH timeout")

    result = screen_click(mock_ssh_session, x=10, y=20)

    assert result.is_error is True
    assert "Click error:" in result.content
    assert "SSH timeout" in result.content


# ============================================================================
# screen_type_text tests
# ============================================================================


def test_screen_type_text_happy_path(mock_ssh_session: MagicMock) -> None:
    """Test screen_type_text successfully types text."""
    mock_ssh_session.execute.return_value = SSHResult(exit_code=0, stdout="", stderr="")

    result = screen_type_text(mock_ssh_session, text="Hello World")

    mock_ssh_session.execute.assert_called_once()
    cmd_arg = mock_ssh_session.execute.call_args[0][0]
    assert "xdotool type -- 'Hello World'" in cmd_arg
    assert "DISPLAY=:0" in cmd_arg

    assert result.is_error is False
    assert result.content == "Typed text: Hello World"


def test_screen_type_text_with_single_quotes(mock_ssh_session: MagicMock) -> None:
    """Test screen_type_text properly escapes single quotes."""
    mock_ssh_session.execute.return_value = SSHResult(exit_code=0, stdout="", stderr="")

    result = screen_type_text(mock_ssh_session, text="It's a test")

    cmd_arg = mock_ssh_session.execute.call_args[0][0]
    assert "It'\\''s a test" in cmd_arg
    assert result.content == "Typed text: It's a test"


def test_screen_type_text_failure(mock_ssh_session: MagicMock) -> None:
    """Test screen_type_text when xdotool fails."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=1,
        stdout="",
        stderr="xdotool error",
    )

    result = screen_type_text(mock_ssh_session, text="test")

    assert result.is_error is True
    assert "Type text failed:" in result.content
    assert "xdotool error" in result.content


# ============================================================================
# screen_key_press tests
# ============================================================================


def test_screen_key_press_happy_path(mock_ssh_session: MagicMock) -> None:
    """Test screen_key_press successfully presses keys."""
    mock_ssh_session.execute.return_value = SSHResult(exit_code=0, stdout="", stderr="")

    result = screen_key_press(mock_ssh_session, keys="Return")

    mock_ssh_session.execute.assert_called_once()
    cmd_arg = mock_ssh_session.execute.call_args[0][0]
    assert "xdotool key Return" in cmd_arg
    assert "DISPLAY=:0" in cmd_arg

    assert result.is_error is False
    assert result.content == "Pressed keys: Return"


def test_screen_key_press_with_modifiers(mock_ssh_session: MagicMock) -> None:
    """Test screen_key_press with modifier keys."""
    mock_ssh_session.execute.return_value = SSHResult(exit_code=0, stdout="", stderr="")

    result = screen_key_press(mock_ssh_session, keys="ctrl+c")

    cmd_arg = mock_ssh_session.execute.call_args[0][0]
    assert "xdotool key ctrl+c" in cmd_arg
    assert result.content == "Pressed keys: ctrl+c"


def test_screen_key_press_failure(mock_ssh_session: MagicMock) -> None:
    """Test screen_key_press when xdotool fails."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=1,
        stdout="",
        stderr="Invalid key name",
    )

    result = screen_key_press(mock_ssh_session, keys="InvalidKey")

    assert result.is_error is True
    assert "Key press failed:" in result.content
    assert "Invalid key name" in result.content


# ============================================================================
# screen_list_windows tests
# ============================================================================


def test_screen_list_windows_happy_path(mock_ssh_session: MagicMock) -> None:
    """Test screen_list_windows successfully lists windows."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=0,
        stdout="0x01400001  0 hostname Terminal\n0x01400002  0 hostname Firefox",
        stderr="",
    )

    result = screen_list_windows(mock_ssh_session)

    mock_ssh_session.execute.assert_called_once()
    cmd_arg = mock_ssh_session.execute.call_args[0][0]
    assert "wmctrl -l" in cmd_arg
    assert "DISPLAY=:0" in cmd_arg

    assert result.is_error is False
    assert "Terminal" in result.content
    assert "Firefox" in result.content


def test_screen_list_windows_no_windows(mock_ssh_session: MagicMock) -> None:
    """Test screen_list_windows when no windows exist."""
    mock_ssh_session.execute.return_value = SSHResult(exit_code=0, stdout="", stderr="")

    result = screen_list_windows(mock_ssh_session)

    assert result.is_error is False
    assert result.content == "No windows found."


def test_screen_list_windows_failure(mock_ssh_session: MagicMock) -> None:
    """Test screen_list_windows when wmctrl fails."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=1,
        stdout="",
        stderr="Cannot open display",
    )

    result = screen_list_windows(mock_ssh_session)

    assert result.is_error is True
    assert "List windows failed:" in result.content
    assert "Cannot open display" in result.content


# ============================================================================
# screen_get_active_window tests
# ============================================================================


def test_screen_get_active_window_happy_path(mock_ssh_session: MagicMock) -> None:
    """Test screen_get_active_window successfully retrieves window name."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=0,
        stdout="Mozilla Firefox\n",
        stderr="",
    )

    result = screen_get_active_window(mock_ssh_session)

    mock_ssh_session.execute.assert_called_once()
    cmd_arg = mock_ssh_session.execute.call_args[0][0]
    assert "xdotool getactivewindow getwindowname" in cmd_arg
    assert "DISPLAY=:0" in cmd_arg

    assert result.is_error is False
    assert result.content == "Mozilla Firefox"


def test_screen_get_active_window_no_window(mock_ssh_session: MagicMock) -> None:
    """Test screen_get_active_window when no active window exists."""
    mock_ssh_session.execute.return_value = SSHResult(exit_code=0, stdout="", stderr="")

    result = screen_get_active_window(mock_ssh_session)

    assert result.is_error is False
    assert result.content == "No active window."


def test_screen_get_active_window_failure(mock_ssh_session: MagicMock) -> None:
    """Test screen_get_active_window when xdotool fails."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=1,
        stdout="",
        stderr="No active window",
    )

    result = screen_get_active_window(mock_ssh_session)

    assert result.is_error is True
    assert "Get active window failed:" in result.content
    assert "No active window" in result.content
