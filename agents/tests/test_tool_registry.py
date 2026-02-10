from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.ssh_session import SSHSessionManager
from agents.services.tool_registry import dispatch_tool_call, get_all_tool_definitions
from agents.types import ToolCall, ToolCategory, ToolContext
from environments.types import ContainerPorts


@pytest.fixture
def test_ports() -> ContainerPorts:
    """Fixture for test container ports."""
    return ContainerPorts(ssh=2222, vnc=5901, playwright_cdp=9223)


@pytest.fixture
def test_context(test_ports: ContainerPorts) -> ToolContext:
    """Fixture for test ToolContext with a mocked SSH session."""
    mock_session = MagicMock(spec=SSHSessionManager)
    return ToolContext(
        ports=test_ports,
        ssh_session=mock_session,
    )


def test_get_all_tool_definitions_count() -> None:
    """Test that get_all_tool_definitions returns all 13 tools."""
    tools = get_all_tool_definitions()
    assert len(tools) == 13


def test_get_all_tool_definitions_categories() -> None:
    """Test that tool definitions include all categories."""
    tools = get_all_tool_definitions()

    shell_tools = [t for t in tools if t.category == ToolCategory.SHELL]
    screen_tools = [t for t in tools if t.category == ToolCategory.SCREEN]
    browser_tools = [t for t in tools if t.category == ToolCategory.BROWSER]

    assert len(shell_tools) == 1
    assert len(screen_tools) == 6
    assert len(browser_tools) == 6


def test_get_all_tool_definitions_valid_structure() -> None:
    """Test that each tool definition has valid name, description, and category."""
    tools = get_all_tool_definitions()

    for tool in tools:
        assert tool.name, "Tool must have a name"
        assert tool.description, "Tool must have a description"
        assert isinstance(tool.category, ToolCategory), "Tool must have valid category"
        assert isinstance(tool.parameters, tuple), "Tool parameters must be a tuple"


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
        from agents.types import ToolResult

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
        from agents.types import ToolResult

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
        from agents.types import ToolResult

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
        from agents.types import ToolResult

        mock_get_url.return_value = ToolResult(
            tool_call_id="",  # Handler returns empty tool_call_id
            content="https://example.com",
            is_error=False,
        )

        result = dispatch_tool_call(tool_call, test_context)

    # Verify the dispatcher sets the correct tool_call_id
    assert result.tool_call_id == "unique_id_12345"


def test_dispatch_tool_call_with_image_base64(test_context: ToolContext) -> None:
    """Test that dispatch_tool_call preserves image_base64 from handler result."""
    tool_call = ToolCall(
        tool_call_id="call_screenshot",
        tool_name="browser_take_screenshot",
        arguments={},
    )

    with patch(
        "agents.services.tool_registry.tools_browser.browser_take_screenshot"
    ) as mock_screenshot:
        from agents.types import ToolResult

        mock_screenshot.return_value = ToolResult(
            tool_call_id="",
            content="Screenshot captured.",
            is_error=False,
            image_base64="base64encodeddata",
        )

        result = dispatch_tool_call(tool_call, test_context)

    assert result.tool_call_id == "call_screenshot"
    assert result.is_error is False
    assert result.image_base64 == "base64encodeddata"


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
        from agents.types import ToolResult

        mock_execute.return_value = ToolResult(
            tool_call_id="",
            content="root\nExit code: 0",
            is_error=False,
        )

        dispatch_tool_call(tool_call, test_context)

    # Verify the ssh_session from context was passed
    mock_execute.assert_called_once_with(test_context.ssh_session, command="whoami")


def test_dispatch_browser_passes_ports(test_context: ToolContext) -> None:
    """Test that browser tool handlers pass ports from context."""
    tool_call = ToolCall(
        tool_call_id="call_nav",
        tool_name="browser_navigate",
        arguments={"url": "https://example.com"},
    )

    with patch(
        "agents.services.tool_registry.tools_browser.browser_navigate"
    ) as mock_navigate:
        from agents.types import ToolResult

        mock_navigate.return_value = ToolResult(
            tool_call_id="",
            content="Navigated",
            is_error=False,
        )

        dispatch_tool_call(tool_call, test_context)

    # Verify ports from context were passed
    mock_navigate.assert_called_once_with(test_context.ports, url="https://example.com")
