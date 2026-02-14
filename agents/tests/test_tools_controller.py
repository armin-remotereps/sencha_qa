from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services import tools_controller
from agents.types import DMRConfig, ToolResult


@pytest.fixture
def mock_vision_config() -> DMRConfig:
    return DMRConfig(
        host="test", port="8080", model="test-vision", temperature=0.0, max_tokens=1000
    )


def test_execute_command_success() -> None:
    with patch(
        "agents.services.tools_controller.controller_run_command"
    ) as mock_controller:
        mock_controller.return_value = {
            "stdout": "hello world",
            "stderr": "",
            "return_code": 0,
        }

        result = tools_controller.execute_command(1, command="echo hello")

        assert result.is_error is False
        assert "hello world" in result.content
        assert "Exit code: 0" in result.content
        mock_controller.assert_called_once_with(1, "echo hello")


def test_execute_command_with_stderr() -> None:
    with patch(
        "agents.services.tools_controller.controller_run_command"
    ) as mock_controller:
        mock_controller.return_value = {
            "stdout": "output",
            "stderr": "warning message",
            "return_code": 0,
        }

        result = tools_controller.execute_command(1, command="ls /fake")

        assert result.is_error is False
        assert "output" in result.content
        assert "STDERR: warning message" in result.content
        assert "Exit code: 0" in result.content


def test_execute_command_non_zero_exit() -> None:
    with patch(
        "agents.services.tools_controller.controller_run_command"
    ) as mock_controller:
        mock_controller.return_value = {
            "stdout": "",
            "stderr": "command not found",
            "return_code": 127,
        }

        result = tools_controller.execute_command(1, command="badcommand")

        assert result.is_error is True
        assert "STDERR: command not found" in result.content
        assert "Exit code: 127" in result.content


def test_execute_command_exception() -> None:
    with patch(
        "agents.services.tools_controller.controller_run_command"
    ) as mock_controller:
        mock_controller.side_effect = Exception("Connection lost")

        result = tools_controller.execute_command(1, command="echo test")

        assert result.is_error is True
        assert "execute_command error:" in result.content
        assert "Connection lost" in result.content


def test_take_screenshot_success(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.tools_controller.controller_screenshot"
    ) as mock_screenshot, patch(
        "agents.services.tools_controller.answer_screenshot_question"
    ) as mock_answer:
        mock_screenshot.return_value = {"image_base64": "base64data"}
        mock_answer.return_value = "The screen shows a desktop with terminal"

        result = tools_controller.take_screenshot(
            1, question="what is on screen?", vision_config=mock_vision_config
        )

        assert result.is_error is False
        assert "desktop with terminal" in result.content
        mock_screenshot.assert_called_once_with(1)
        mock_answer.assert_called_once_with(
            mock_vision_config, "base64data", "what is on screen?"
        )


def test_take_screenshot_with_callback(mock_vision_config: DMRConfig) -> None:
    mock_callback = MagicMock()

    with patch(
        "agents.services.tools_controller.controller_screenshot"
    ) as mock_screenshot, patch(
        "agents.services.tools_controller.answer_screenshot_question"
    ) as mock_answer:
        mock_screenshot.return_value = {"image_base64": "base64data"}
        mock_answer.return_value = "Desktop visible"

        result = tools_controller.take_screenshot(
            1,
            question="describe screen",
            vision_config=mock_vision_config,
            on_screenshot=mock_callback,
        )

        assert result.is_error is False
        mock_callback.assert_called_once_with("base64data", "take_screenshot")


def test_click_success(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder, patch(
        "agents.services.tools_controller.controller_click"
    ) as mock_click:
        mock_finder.return_value = (250, 180)

        result = tools_controller.click(
            1, description="the OK button", vision_config=mock_vision_config
        )

        assert result.is_error is False
        assert "Clicked element at (250, 180): the OK button" in result.content
        mock_finder.assert_called_once_with(
            1, "the OK button", mock_vision_config, on_screenshot=None
        )
        mock_click.assert_called_once_with(1, 250, 180)


def test_click_with_callback(mock_vision_config: DMRConfig) -> None:
    mock_callback = MagicMock()

    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder, patch(
        "agents.services.tools_controller.controller_click"
    ) as mock_click:
        mock_finder.return_value = (100, 200)

        result = tools_controller.click(
            1,
            description="submit button",
            vision_config=mock_vision_config,
            on_screenshot=mock_callback,
        )

        assert result.is_error is False
        mock_finder.assert_called_once_with(
            1, "submit button", mock_vision_config, on_screenshot=mock_callback
        )


def test_click_element_not_found(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder:
        from agents.exceptions import ElementNotFoundError

        mock_finder.side_effect = ElementNotFoundError("Element not found")

        result = tools_controller.click(
            1, description="nonexistent button", vision_config=mock_vision_config
        )

        assert result.is_error is True
        assert "click error:" in result.content
        assert "Element not found" in result.content


def test_type_text_success() -> None:
    with patch("agents.services.tools_controller.controller_type_text") as mock_type:
        result = tools_controller.type_text(1, text="hello world")

        assert result.is_error is False
        assert "Typed text: hello world" in result.content
        mock_type.assert_called_once_with(1, "hello world")


def test_type_text_exception() -> None:
    with patch("agents.services.tools_controller.controller_type_text") as mock_type:
        mock_type.side_effect = RuntimeError("Keyboard error")

        result = tools_controller.type_text(1, text="test")

        assert result.is_error is True
        assert "type_text error:" in result.content
        assert "Keyboard error" in result.content


def test_key_press_success() -> None:
    with patch("agents.services.tools_controller.controller_key_press") as mock_key:
        result = tools_controller.key_press(1, keys="Return")

        assert result.is_error is False
        assert "Pressed keys: Return" in result.content
        mock_key.assert_called_once_with(1, "Return")


def test_key_press_combination() -> None:
    with patch("agents.services.tools_controller.controller_key_press") as mock_key:
        result = tools_controller.key_press(1, keys="ctrl+c")

        assert result.is_error is False
        assert "Pressed keys: ctrl+c" in result.content
        mock_key.assert_called_once_with(1, "ctrl+c")


def test_key_press_exception() -> None:
    with patch("agents.services.tools_controller.controller_key_press") as mock_key:
        mock_key.side_effect = ValueError("Invalid key")

        result = tools_controller.key_press(1, keys="BadKey")

        assert result.is_error is True
        assert "key_press error:" in result.content
        assert "Invalid key" in result.content


def test_hover_success(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder, patch(
        "agents.services.tools_controller.controller_hover"
    ) as mock_hover:
        mock_finder.return_value = (300, 150)

        result = tools_controller.hover(
            1, description="settings menu", vision_config=mock_vision_config
        )

        assert result.is_error is False
        assert "Hovered over element at (300, 150): settings menu" in result.content
        mock_finder.assert_called_once_with(
            1, "settings menu", mock_vision_config, on_screenshot=None
        )
        mock_hover.assert_called_once_with(1, 300, 150)


def test_hover_with_callback(mock_vision_config: DMRConfig) -> None:
    mock_callback = MagicMock()

    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder, patch(
        "agents.services.tools_controller.controller_hover"
    ) as mock_hover:
        mock_finder.return_value = (400, 250)

        result = tools_controller.hover(
            1,
            description="menu item",
            vision_config=mock_vision_config,
            on_screenshot=mock_callback,
        )

        assert result.is_error is False
        mock_finder.assert_called_once_with(
            1, "menu item", mock_vision_config, on_screenshot=mock_callback
        )


def test_hover_exception(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder:
        mock_finder.side_effect = TimeoutError("Timed out")

        result = tools_controller.hover(
            1, description="element", vision_config=mock_vision_config
        )

        assert result.is_error is True
        assert "hover error:" in result.content
        assert "Timed out" in result.content


def test_drag_success(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder, patch(
        "agents.services.tools_controller.controller_drag"
    ) as mock_drag:
        mock_finder.side_effect = [(50, 50), (200, 200)]

        result = tools_controller.drag(
            1,
            start_description="file icon",
            end_description="trash icon",
            vision_config=mock_vision_config,
        )

        assert result.is_error is False
        assert "Dragged from (50, 50) to (200, 200)" in result.content
        assert mock_finder.call_count == 2
        mock_drag.assert_called_once_with(1, 50, 50, 200, 200)


def test_drag_with_callback(mock_vision_config: DMRConfig) -> None:
    mock_callback = MagicMock()

    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder, patch(
        "agents.services.tools_controller.controller_drag"
    ) as mock_drag:
        mock_finder.side_effect = [(100, 100), (300, 300)]

        result = tools_controller.drag(
            1,
            start_description="slider",
            end_description="target",
            vision_config=mock_vision_config,
            on_screenshot=mock_callback,
        )

        assert result.is_error is False
        assert mock_finder.call_count == 2
        # Both calls should pass the callback
        for call in mock_finder.call_args_list:
            assert call[1]["on_screenshot"] == mock_callback


def test_drag_start_element_not_found(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder:
        from agents.exceptions import ElementNotFoundError

        mock_finder.side_effect = ElementNotFoundError("Start element not found")

        result = tools_controller.drag(
            1,
            start_description="source",
            end_description="dest",
            vision_config=mock_vision_config,
        )

        assert result.is_error is True
        assert "drag error:" in result.content
        assert "Start element not found" in result.content


def test_drag_end_element_not_found(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.tools_controller.find_element_coordinates"
    ) as mock_finder:
        from agents.exceptions import ElementNotFoundError

        # First call succeeds, second fails
        mock_finder.side_effect = [
            (100, 100),
            ElementNotFoundError("End element not found"),
        ]

        result = tools_controller.drag(
            1,
            start_description="source",
            end_description="dest",
            vision_config=mock_vision_config,
        )

        assert result.is_error is True
        assert "drag error:" in result.content
        assert "End element not found" in result.content
