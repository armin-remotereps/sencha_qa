from unittest.mock import MagicMock, patch

import pytest

from controller_client.exceptions import ExecutionError
from controller_client.executor import (
    _is_background_command,
    execute_click,
    execute_command,
    execute_drag,
    execute_hover,
    execute_key_press,
    execute_screenshot,
    execute_type_text,
)
from controller_client.protocol import (
    ClickPayload,
    DragPayload,
    HoverPayload,
    KeyPressPayload,
    RunCommandPayload,
    TypeTextPayload,
)


class TestExecuteClick:
    @patch("controller_client.executor.pyautogui")
    def test_left_click(self, mock_pyautogui: MagicMock) -> None:
        payload = ClickPayload(x=100, y=200, button="left")
        result = execute_click(payload)
        mock_pyautogui.click.assert_called_once_with(x=100, y=200, button="left")
        assert result.success is True
        assert result.duration_ms >= 0

    @patch("controller_client.executor.pyautogui")
    def test_right_click(self, mock_pyautogui: MagicMock) -> None:
        payload = ClickPayload(x=50, y=75, button="right")
        result = execute_click(payload)
        mock_pyautogui.click.assert_called_once_with(x=50, y=75, button="right")
        assert result.success is True

    @patch("controller_client.executor.pyautogui")
    def test_click_failure(self, mock_pyautogui: MagicMock) -> None:
        mock_pyautogui.click.side_effect = RuntimeError("display error")
        payload = ClickPayload(x=0, y=0, button="left")
        with pytest.raises(ExecutionError, match="Click failed"):
            execute_click(payload)


class TestExecuteHover:
    @patch("controller_client.executor.pyautogui")
    def test_hover(self, mock_pyautogui: MagicMock) -> None:
        payload = HoverPayload(x=300, y=400)
        result = execute_hover(payload)
        mock_pyautogui.moveTo.assert_called_once_with(x=300, y=400)
        assert result.success is True

    @patch("controller_client.executor.pyautogui")
    def test_hover_failure(self, mock_pyautogui: MagicMock) -> None:
        mock_pyautogui.moveTo.side_effect = RuntimeError("fail")
        payload = HoverPayload(x=0, y=0)
        with pytest.raises(ExecutionError, match="Hover failed"):
            execute_hover(payload)


class TestExecuteDrag:
    @patch("controller_client.executor.pyautogui")
    def test_drag(self, mock_pyautogui: MagicMock) -> None:
        payload = DragPayload(
            start_x=10, start_y=20, end_x=110, end_y=120, button="left", duration=0.5
        )
        result = execute_drag(payload)
        mock_pyautogui.moveTo.assert_called_once_with(x=10, y=20)
        mock_pyautogui.drag.assert_called_once_with(
            xOffset=100, yOffset=100, duration=0.5, button="left"
        )
        assert result.success is True

    @patch("controller_client.executor.pyautogui")
    def test_drag_failure(self, mock_pyautogui: MagicMock) -> None:
        mock_pyautogui.moveTo.side_effect = RuntimeError("fail")
        payload = DragPayload(
            start_x=0, start_y=0, end_x=1, end_y=1, button="left", duration=0.1
        )
        with pytest.raises(ExecutionError, match="Drag failed"):
            execute_drag(payload)


class TestExecuteTypeText:
    @patch("controller_client.executor.pyautogui")
    def test_type_text(self, mock_pyautogui: MagicMock) -> None:
        payload = TypeTextPayload(text="hello world", interval=0.05)
        result = execute_type_text(payload)
        mock_pyautogui.typewrite.assert_called_once_with("hello world", interval=0.05)
        assert result.success is True
        assert "11 characters" in result.message

    @patch("controller_client.executor.pyautogui")
    def test_type_text_failure(self, mock_pyautogui: MagicMock) -> None:
        mock_pyautogui.typewrite.side_effect = RuntimeError("fail")
        payload = TypeTextPayload(text="x", interval=0.0)
        with pytest.raises(ExecutionError, match="Type text failed"):
            execute_type_text(payload)


class TestExecuteKeyPress:
    @patch("controller_client.executor.pyautogui")
    def test_single_key(self, mock_pyautogui: MagicMock) -> None:
        payload = KeyPressPayload(keys="Return")
        result = execute_key_press(payload)
        mock_pyautogui.press.assert_called_once_with("Return")
        assert result.success is True

    @patch("controller_client.executor.pyautogui")
    def test_hotkey_combo(self, mock_pyautogui: MagicMock) -> None:
        payload = KeyPressPayload(keys="ctrl+c")
        result = execute_key_press(payload)
        mock_pyautogui.hotkey.assert_called_once_with("ctrl", "c")
        assert result.success is True

    @patch("controller_client.executor.pyautogui")
    def test_three_key_combo(self, mock_pyautogui: MagicMock) -> None:
        payload = KeyPressPayload(keys="ctrl+shift+s")
        result = execute_key_press(payload)
        mock_pyautogui.hotkey.assert_called_once_with("ctrl", "shift", "s")

    @patch("controller_client.executor.pyautogui")
    def test_key_press_failure(self, mock_pyautogui: MagicMock) -> None:
        mock_pyautogui.press.side_effect = RuntimeError("fail")
        payload = KeyPressPayload(keys="F1")
        with pytest.raises(ExecutionError, match="Key press failed"):
            execute_key_press(payload)


class TestIsBackgroundCommand:
    def test_trailing_ampersand(self) -> None:
        assert _is_background_command("gnome-calculator &") is True

    def test_trailing_ampersand_no_space(self) -> None:
        assert _is_background_command("gnome-calculator&") is True

    def test_trailing_ampersand_with_whitespace(self) -> None:
        assert _is_background_command("gnome-calculator &  ") is True

    def test_no_ampersand(self) -> None:
        assert _is_background_command("echo hello") is False

    def test_double_ampersand_is_not_background(self) -> None:
        assert _is_background_command("echo a && echo b") is False

    def test_ampersand_in_middle_is_not_background(self) -> None:
        assert _is_background_command("echo a & echo b") is False


class TestExecuteBackgroundCommand:
    @patch("controller_client.executor.subprocess.Popen")
    def test_background_command_returns_immediately(
        self, mock_popen: MagicMock
    ) -> None:
        payload = RunCommandPayload(command="gnome-calculator &")
        result = execute_command(payload)
        assert result.success is True
        assert result.return_code == 0
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["start_new_session"] is True

    @patch("controller_client.executor.subprocess.Popen")
    def test_background_command_failure(self, mock_popen: MagicMock) -> None:
        mock_popen.side_effect = OSError("spawn failed")
        payload = RunCommandPayload(command="badapp &")
        with pytest.raises(ExecutionError, match="Background command failed"):
            execute_command(payload)


class TestExecuteCommand:
    @patch("controller_client.executor.subprocess.run")
    def test_successful_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="hello world\n", stderr=""
        )
        payload = RunCommandPayload(command="echo hello world")
        result = execute_command(payload)
        assert result.success is True
        assert result.stdout == "hello world\n"
        assert result.stderr == ""
        assert result.return_code == 0
        assert result.duration_ms >= 0
        mock_run.assert_called_once_with(
            "echo hello world",
            shell=True,
            capture_output=True,
            text=True,
        )

    @patch("controller_client.executor.subprocess.run")
    def test_failing_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found\n")
        payload = RunCommandPayload(command="false")
        result = execute_command(payload)
        assert result.success is False
        assert result.return_code == 1
        assert result.stderr == "not found\n"

    @patch("controller_client.executor.subprocess.run")
    def test_exception_raises_execution_error(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = OSError("no such file")
        payload = RunCommandPayload(command="badcmd")
        with pytest.raises(ExecutionError, match="Command execution failed"):
            execute_command(payload)


class TestExecuteScreenshot:
    @patch("controller_client.executor.pyautogui")
    def test_screenshot(self, mock_pyautogui: MagicMock) -> None:
        from PIL import Image

        mock_image = Image.new("RGB", (1920, 1080), color="red")
        mock_pyautogui.screenshot.return_value = mock_image

        result = execute_screenshot()
        assert result.success is True
        assert result.width == 1920
        assert result.height == 1080
        assert result.format == "png"
        assert len(result.image_base64) > 0

    @patch("controller_client.executor.pyautogui")
    def test_screenshot_failure(self, mock_pyautogui: MagicMock) -> None:
        mock_pyautogui.screenshot.side_effect = RuntimeError("no display")
        with pytest.raises(ExecutionError, match="Screenshot failed"):
            execute_screenshot()
