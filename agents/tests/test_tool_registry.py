from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.playwright_session import PlaywrightSessionManager
from agents.services.ssh_session import SSHSessionManager
from agents.services.tool_registry import dispatch_tool_call, get_all_tool_definitions
from agents.services.vnc_session import VncSessionManager
from agents.types import DMRConfig, ToolCall, ToolCategory, ToolContext, ToolResult
from environments.types import ContainerPorts


@pytest.fixture
def test_ports() -> ContainerPorts:
    """Fixture for test container ports."""
    return ContainerPorts(ssh=2222, vnc=5901, playwright_cdp=9223)


@pytest.fixture
def test_context(test_ports: ContainerPorts) -> ToolContext:
    """Fixture for test ToolContext with mocked sessions."""
    mock_ssh = MagicMock(spec=SSHSessionManager)
    mock_pw = MagicMock(spec=PlaywrightSessionManager)
    mock_vnc = MagicMock(spec=VncSessionManager)
    mock_vision = DMRConfig(
        host="test", port="8080", model="test-vision", temperature=0.0, max_tokens=1000
    )
    return ToolContext(
        ports=test_ports,
        ssh_session=mock_ssh,
        playwright_session=mock_pw,
        vnc_session=mock_vnc,
        vision_config=mock_vision,
    )


def test_get_all_tool_definitions_count() -> None:
    """Test that get_all_tool_definitions returns all 19 tools."""
    tools = get_all_tool_definitions()
    assert len(tools) == 19


def test_get_all_tool_definitions_categories() -> None:
    """Test that tool definitions include all categories."""
    tools = get_all_tool_definitions()

    shell_tools = [t for t in tools if t.category == ToolCategory.SHELL]
    screen_tools = [t for t in tools if t.category == ToolCategory.SCREEN]
    browser_tools = [t for t in tools if t.category == ToolCategory.BROWSER]
    vnc_tools = [t for t in tools if t.category == ToolCategory.VNC]

    assert len(shell_tools) == 1
    assert len(screen_tools) == 6
    assert len(browser_tools) == 7
    assert len(vnc_tools) == 5


def test_get_all_tool_definitions_valid_structure() -> None:
    """Test that each tool definition has valid name, description, and category."""
    tools = get_all_tool_definitions()

    for tool in tools:
        assert tool.name, "Tool must have a name"
        assert tool.description, "Tool must have a description"
        assert isinstance(tool.category, ToolCategory), "Tool must have valid category"
        assert isinstance(tool.parameters, tuple), "Tool parameters must be a tuple"


def test_get_all_tool_definitions_vnc_tool_names() -> None:
    """Test that VNC tool names are correct."""
    tools = get_all_tool_definitions()
    vnc_names = {t.name for t in tools if t.category == ToolCategory.VNC}
    expected = {
        "vnc_take_screenshot",
        "vnc_click",
        "vnc_type",
        "vnc_hover",
        "vnc_key_press",
    }
    assert vnc_names == expected


def test_dispatch_tool_call_execute_command(test_context: ToolContext) -> None:
    """Test dispatch_tool_call correctly dispatches execute_command."""
    tool_call = ToolCall(
        tool_call_id="call_123",
        tool_name="execute_command",
        arguments={"command": "echo hello"},
    )

    with patch(
        "agents.services.tool_registry.tools_shell.execute_command"
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
    mock_execute.assert_called_once()


def test_dispatch_tool_call_browser_navigate(test_context: ToolContext) -> None:
    """Test dispatch_tool_call correctly dispatches browser_navigate."""
    tool_call = ToolCall(
        tool_call_id="call_456",
        tool_name="browser_navigate",
        arguments={"url": "https://example.com"},
    )

    with patch(
        "agents.services.tool_registry.tools_browser.browser_navigate"
    ) as mock_navigate:
        mock_navigate.return_value = ToolResult(
            tool_call_id="",
            content="Navigated to https://example.com. Page title: Example",
            is_error=False,
        )

        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_456"
    assert result.is_error is False
    assert "Navigated to" in result.content
    mock_navigate.assert_called_once()


def test_dispatch_tool_call_screen_click(test_context: ToolContext) -> None:
    """Test dispatch_tool_call correctly dispatches screen_click."""
    tool_call = ToolCall(
        tool_call_id="call_789",
        tool_name="screen_click",
        arguments={"x": 100, "y": 200, "button": 1},
    )

    with patch("agents.services.tool_registry.tools_screen.screen_click") as mock_click:
        mock_click.return_value = ToolResult(
            tool_call_id="",
            content="Clicked at (100, 200) with button 1.",
            is_error=False,
        )

        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_789"
    assert result.is_error is False
    assert "Clicked at" in result.content
    mock_click.assert_called_once()


def test_dispatch_tool_call_unknown_tool(test_context: ToolContext) -> None:
    """Test dispatch_tool_call returns error for unknown tool."""
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
    """Test that dispatch_tool_call correctly sets tool_call_id on result."""
    tool_call = ToolCall(
        tool_call_id="unique_id_12345",
        tool_name="browser_get_url",
        arguments={},
    )

    with patch(
        "agents.services.tool_registry.tools_browser.browser_get_url"
    ) as mock_get_url:
        mock_get_url.return_value = ToolResult(
            tool_call_id="",  # Handler returns empty tool_call_id
            content="https://example.com",
            is_error=False,
        )

        result = dispatch_tool_call(tool_call, test_context)

    # Verify the dispatcher sets the correct tool_call_id
    assert result.tool_call_id == "unique_id_12345"


def test_dispatch_browser_hover(test_context: ToolContext) -> None:
    """Test dispatch_tool_call correctly dispatches browser_hover."""
    tool_call = ToolCall(
        tool_call_id="call_hover",
        tool_name="browser_hover",
        arguments={"description": "the settings menu"},
    )

    with patch(
        "agents.services.tool_registry.tools_browser.browser_hover"
    ) as mock_hover:
        mock_hover.return_value = ToolResult(
            tool_call_id="",
            content="Hovered over element: the settings menu",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_hover"
    assert result.is_error is False
    assert "Hovered over" in result.content


def test_dispatch_shell_passes_ssh_session(test_context: ToolContext) -> None:
    """Test that shell tool handlers pass ssh_session from context."""
    tool_call = ToolCall(
        tool_call_id="call_ssh",
        tool_name="execute_command",
        arguments={"command": "whoami"},
    )

    with patch(
        "agents.services.tool_registry.tools_shell.execute_command"
    ) as mock_execute:
        mock_execute.return_value = ToolResult(
            tool_call_id="",
            content="root\nExit code: 0",
            is_error=False,
        )

        dispatch_tool_call(tool_call, test_context)

    # Verify the ssh_session from context was passed
    mock_execute.assert_called_once_with(test_context.ssh_session, command="whoami")


def test_dispatch_browser_passes_playwright_session(test_context: ToolContext) -> None:
    """Test that browser tool handlers pass playwright_session from context."""
    tool_call = ToolCall(
        tool_call_id="call_nav",
        tool_name="browser_navigate",
        arguments={"url": "https://example.com"},
    )

    with patch(
        "agents.services.tool_registry.tools_browser.browser_navigate"
    ) as mock_navigate:
        mock_navigate.return_value = ToolResult(
            tool_call_id="",
            content="Navigated",
            is_error=False,
        )
        dispatch_tool_call(tool_call, test_context)

    mock_navigate.assert_called_once_with(
        test_context.playwright_session,
        url="https://example.com",
        vision_config=test_context.vision_config,
        on_screenshot=test_context.on_screenshot,
    )


# ============================================================================
# VNC dispatch tests
# ============================================================================


def test_dispatch_vnc_click(test_context: ToolContext) -> None:
    """Test dispatch_tool_call correctly dispatches vnc_click."""
    tool_call = ToolCall(
        tool_call_id="call_vnc_click",
        tool_name="vnc_click",
        arguments={"description": "the OK button"},
    )

    with patch("agents.services.tool_registry.tools_vnc.vnc_click") as mock_click:
        mock_click.return_value = ToolResult(
            tool_call_id="",
            content="Clicked element at (250, 180): the OK button",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_vnc_click"
    assert result.is_error is False
    assert "OK button" in result.content
    mock_click.assert_called_once_with(
        test_context.vnc_session,
        description="the OK button",
        vision_config=test_context.vision_config,
        on_screenshot=test_context.on_screenshot,
    )


def test_dispatch_vnc_type(test_context: ToolContext) -> None:
    """Test dispatch_tool_call correctly dispatches vnc_type."""
    tool_call = ToolCall(
        tool_call_id="call_vnc_type",
        tool_name="vnc_type",
        arguments={"description": "search box", "text": "hello"},
    )

    with patch("agents.services.tool_registry.tools_vnc.vnc_type") as mock_type:
        mock_type.return_value = ToolResult(
            tool_call_id="",
            content="Typed 'hello' into element",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_vnc_type"
    assert result.is_error is False
    mock_type.assert_called_once_with(
        test_context.vnc_session,
        description="search box",
        text="hello",
        vision_config=test_context.vision_config,
        on_screenshot=test_context.on_screenshot,
    )


def test_dispatch_vnc_key_press(test_context: ToolContext) -> None:
    """Test dispatch_tool_call correctly dispatches vnc_key_press."""
    tool_call = ToolCall(
        tool_call_id="call_vnc_key",
        tool_name="vnc_key_press",
        arguments={"keys": "Return"},
    )

    with patch("agents.services.tool_registry.tools_vnc.vnc_key_press") as mock_key:
        mock_key.return_value = ToolResult(
            tool_call_id="",
            content="Pressed keys via VNC: Return",
            is_error=False,
        )
        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_vnc_key"
    assert result.is_error is False
    mock_key.assert_called_once_with(
        test_context.vnc_session,
        keys="Return",
    )


def test_dispatch_vnc_click_no_vision_config(test_ports: ContainerPorts) -> None:
    """VNC click returns error when vision config is None."""
    context = ToolContext(
        ports=test_ports,
        ssh_session=MagicMock(spec=SSHSessionManager),
        playwright_session=MagicMock(spec=PlaywrightSessionManager),
        vnc_session=MagicMock(spec=VncSessionManager),
        vision_config=None,
    )
    tool_call = ToolCall(
        tool_call_id="call_no_vision",
        tool_name="vnc_click",
        arguments={"description": "button"},
    )

    result = dispatch_tool_call(tool_call, context)

    assert result.is_error is True
    assert "Vision model not configured" in result.content
