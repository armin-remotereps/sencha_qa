from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.tool_registry import dispatch_tool_call, get_all_tool_definitions
from agents.types import DMRConfig, ToolCall, ToolCategory, ToolContext, ToolResult


@pytest.fixture
def test_context() -> ToolContext:
    mock_vision = DMRConfig(
        host="test", port="8080", model="test-vision", temperature=0.0, max_tokens=1000
    )
    return ToolContext(
        project_id=1,
        vision_config=mock_vision,
    )


def test_get_all_tool_definitions_count() -> None:
    tools = get_all_tool_definitions()
    assert len(tools) == 15


def test_get_all_tool_definitions_categories() -> None:
    tools = get_all_tool_definitions()
    categories = {tool.category for tool in tools}
    assert ToolCategory.CONTROLLER in categories
    assert ToolCategory.BROWSER in categories


def test_get_all_tool_definitions_valid_structure() -> None:
    tools = get_all_tool_definitions()
    for tool in tools:
        assert tool.name, "Tool must have a name"
        assert tool.description, "Tool must have a description"
        assert isinstance(tool.category, ToolCategory)
        assert isinstance(tool.parameters, tuple)


def test_get_all_tool_definitions_tool_names() -> None:
    tools = get_all_tool_definitions()
    names = {t.name for t in tools}
    expected = {
        "execute_command",
        "take_screenshot",
        "click",
        "type_text",
        "key_press",
        "hover",
        "drag",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_hover",
        "browser_get_page_content",
        "browser_get_url",
        "browser_take_screenshot",
        "browser_download",
    }
    assert names == expected


def test_dispatch_tool_call_execute_command(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_123",
        tool_name="execute_command",
        arguments={"command": "echo hello"},
    )
    with patch(
        "agents.services.tool_registry.tools_controller.execute_command"
    ) as mock_execute:
        mock_execute.return_value = ToolResult(
            tool_call_id="",
            content="hello\nExit code: 0",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_123"
    assert result.is_error is False
    assert "hello" in result.content
    mock_execute.assert_called_once_with(1, command="echo hello")


def test_dispatch_tool_call_click(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_click",
        tool_name="click",
        arguments={"description": "the OK button"},
    )
    with patch("agents.services.tool_registry.tools_controller.click") as mock_click:
        mock_click.return_value = ToolResult(
            tool_call_id="",
            content="Clicked element at (250, 180): the OK button",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_click"
    assert result.is_error is False
    assert "OK button" in result.content
    mock_click.assert_called_once_with(
        1,
        description="the OK button",
        vision_config=test_context.vision_config,
        on_screenshot=test_context.on_screenshot,
    )


def test_dispatch_tool_call_type_text(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_type",
        tool_name="type_text",
        arguments={"text": "hello world"},
    )
    with patch("agents.services.tool_registry.tools_controller.type_text") as mock_type:
        mock_type.return_value = ToolResult(
            tool_call_id="",
            content="Typed text: hello world",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_type"
    assert result.is_error is False
    mock_type.assert_called_once_with(1, text="hello world")


def test_dispatch_tool_call_key_press(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_key",
        tool_name="key_press",
        arguments={"keys": "Return"},
    )
    with patch("agents.services.tool_registry.tools_controller.key_press") as mock_key:
        mock_key.return_value = ToolResult(
            tool_call_id="",
            content="Pressed keys: Return",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_key"
    assert result.is_error is False
    mock_key.assert_called_once_with(1, keys="Return")


def test_dispatch_tool_call_hover(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_hover",
        tool_name="hover",
        arguments={"description": "the settings menu"},
    )
    with patch("agents.services.tool_registry.tools_controller.hover") as mock_hover:
        mock_hover.return_value = ToolResult(
            tool_call_id="",
            content="Hovered over element at (100, 200): the settings menu",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_hover"
    assert result.is_error is False
    mock_hover.assert_called_once_with(
        1,
        description="the settings menu",
        vision_config=test_context.vision_config,
        on_screenshot=test_context.on_screenshot,
    )


def test_dispatch_tool_call_drag(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_drag",
        tool_name="drag",
        arguments={"start_description": "file icon", "end_description": "trash icon"},
    )
    with patch("agents.services.tool_registry.tools_controller.drag") as mock_drag:
        mock_drag.return_value = ToolResult(
            tool_call_id="",
            content="Dragged from (50, 50) to (200, 200)",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_drag"
    assert result.is_error is False
    mock_drag.assert_called_once_with(
        1,
        start_description="file icon",
        end_description="trash icon",
        vision_config=test_context.vision_config,
        on_screenshot=test_context.on_screenshot,
    )


def test_dispatch_tool_call_take_screenshot(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_screenshot",
        tool_name="take_screenshot",
        arguments={"question": "what is on screen?"},
    )
    with patch(
        "agents.services.tool_registry.tools_controller.take_screenshot"
    ) as mock_screenshot:
        mock_screenshot.return_value = ToolResult(
            tool_call_id="",
            content="The screen shows a desktop with a terminal window open",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_screenshot"
    assert result.is_error is False
    assert "desktop" in result.content
    mock_screenshot.assert_called_once_with(
        1,
        question="what is on screen?",
        vision_config=test_context.vision_config,
        on_screenshot=test_context.on_screenshot,
    )


def test_dispatch_tool_call_unknown_tool(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_999",
        tool_name="nonexistent_tool",
        arguments={},
    )
    result = dispatch_tool_call(tool_call, test_context)
    assert result.tool_call_id == "call_999"
    assert result.is_error is True
    assert "Unknown tool" in result.content
    assert "nonexistent_tool" in result.content


def test_dispatch_tool_call_preserves_tool_call_id(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="unique_id_12345",
        tool_name="execute_command",
        arguments={"command": "ls"},
    )
    with patch(
        "agents.services.tool_registry.tools_controller.execute_command"
    ) as mock_cmd:
        mock_cmd.return_value = ToolResult(
            tool_call_id="",
            content="file1\nfile2\nExit code: 0",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)
    assert result.tool_call_id == "unique_id_12345"


def test_dispatch_click_no_vision_config() -> None:
    context = ToolContext(project_id=1, vision_config=None)
    tool_call = ToolCall(
        tool_call_id="call_no_vision",
        tool_name="click",
        arguments={"description": "button"},
    )
    result = dispatch_tool_call(tool_call, context)
    assert result.is_error is True
    assert "Vision model not configured" in result.content


def test_dispatch_tool_call_browser_download(test_context: ToolContext) -> None:
    tool_call = ToolCall(
        tool_call_id="call_download",
        tool_name="browser_download",
        arguments={"url": "https://example.com/file.exe", "save_path": "/tmp/file.exe"},
    )
    with patch(
        "agents.services.tool_registry.tools_controller.browser_download"
    ) as mock_download:
        mock_download.return_value = ToolResult(
            tool_call_id="",
            content="Downloaded to /tmp/file.exe (1024 bytes)",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_download"
    assert result.is_error is False
    assert "Downloaded" in result.content
    mock_download.assert_called_once_with(
        1, url="https://example.com/file.exe", save_path="/tmp/file.exe"
    )


def test_dispatch_tool_call_browser_download_default_save_path(
    test_context: ToolContext,
) -> None:
    tool_call = ToolCall(
        tool_call_id="call_download_default",
        tool_name="browser_download",
        arguments={"url": "https://example.com/installer.dmg"},
    )
    with patch(
        "agents.services.tool_registry.tools_controller.browser_download"
    ) as mock_download:
        mock_download.return_value = ToolResult(
            tool_call_id="",
            content="Downloaded to /home/user/Downloads/installer.dmg (2048 bytes)",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_download_default"
    assert result.is_error is False
    mock_download.assert_called_once_with(
        1, url="https://example.com/installer.dmg", save_path=""
    )


def test_dispatch_take_screenshot_no_vision_config() -> None:
    context = ToolContext(project_id=1, vision_config=None)
    tool_call = ToolCall(
        tool_call_id="call_no_vision",
        tool_name="take_screenshot",
        arguments={"question": "what is on screen?"},
    )
    result = dispatch_tool_call(tool_call, context)
    assert result.is_error is True
    assert "Vision model not configured" in result.content
