from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from agents.services.agent_loop import (
    _build_role_description,
    _build_task_section,
    _build_tool_guidelines,
    _build_tool_result_message,
    build_agent_config,
    build_system_prompt,
    run_agent,
)
from agents.services.agent_resource_manager import AgentResourceManager
from agents.services.playwright_session import PlaywrightSessionManager
from agents.types import (
    AgentConfig,
    AgentStopReason,
    ChatMessage,
    DMRConfig,
    DMRResponse,
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    ToolParameter,
    ToolResult,
)
from environments.types import ContainerPorts


@pytest.fixture
def mock_ports() -> ContainerPorts:
    """Fixture for test ports."""
    return ContainerPorts(ssh=2222, vnc=5900, playwright_cdp=9222)


@pytest.fixture
def mock_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Fixture for mock tool definitions."""
    return (
        ToolDefinition(
            name="test_tool",
            description="A test tool",
            category=ToolCategory.SHELL,
            parameters=(
                ToolParameter(
                    name="arg1",
                    type="string",
                    description="First argument",
                    required=True,
                ),
            ),
        ),
    )


def test_build_system_prompt() -> None:
    """Test that build_system_prompt includes the task description."""
    task = "Install Firefox and open Google"
    prompt = build_system_prompt(task)

    assert "Install Firefox and open Google" in prompt
    assert "AI test automation agent" in prompt
    assert "SHELL" in prompt
    assert "SCREEN" in prompt
    assert "BROWSER" in prompt
    assert "VNC" in prompt
    assert "Ubuntu 24.04 with XFCE4" in prompt
    assert "natural-language descriptions" in prompt
    assert "browser_hover" in prompt
    assert "question" in prompt.lower()
    assert "ALREADY RUNNING" in prompt
    assert "browser_navigate" in prompt
    assert "Do NOT try to install or launch" in prompt
    assert "root" in prompt
    assert "sudo" in prompt


def test_build_system_prompt_vnc_tools() -> None:
    """Test that build_system_prompt includes VNC tool examples."""
    prompt = build_system_prompt("test task")

    assert "vnc_take_screenshot" in prompt
    assert "vnc_click" in prompt
    assert "vnc_type" in prompt
    assert "vnc_hover" in prompt
    assert "vnc_key_press" in prompt
    assert "vision-based" in prompt.lower() or "vision AI" in prompt


@override_settings(
    AGENT_MAX_ITERATIONS=50,
    AGENT_TIMEOUT_SECONDS=600,
    DMR_HOST="test-dmr",
    DMR_PORT="8080",
    DMR_MODEL="test-model",
    DMR_VISION_MODEL="test-vision-model",
    DMR_TEMPERATURE=0.7,
    DMR_MAX_TOKENS=4096,
)
def test_build_agent_config_from_settings() -> None:
    """Test that build_agent_config reads from Django settings."""
    config = build_agent_config()

    assert config.max_iterations == 50
    assert config.timeout_seconds == 600
    assert config.dmr.host == "test-dmr"
    assert config.dmr.port == "8080"
    assert config.dmr.model == "test-model"
    assert config.dmr.temperature == 0.7
    assert config.dmr.max_tokens == 4096
    assert config.vision_dmr is not None
    assert config.vision_dmr.model == "test-vision-model"


@override_settings(
    AGENT_MAX_ITERATIONS=10,
    AGENT_TIMEOUT_SECONDS=300,
    DMR_HOST="dmr",
    DMR_PORT="8080",
    DMR_MODEL="default-model",
    DMR_VISION_MODEL="default-vision",
)
def test_build_agent_config_with_model_override() -> None:
    """Test that build_agent_config allows model override."""
    config = build_agent_config(
        model="custom-action-model", vision_model="custom-vision-model"
    )

    assert config.dmr.model == "custom-action-model"
    assert config.vision_dmr is not None
    assert config.vision_dmr.model == "custom-vision-model"
    assert config.max_iterations == 10
    assert config.timeout_seconds == 300


def _make_mock_resources() -> MagicMock:
    """Helper to create mock resources with ssh, playwright, and vnc."""
    mock_resources = MagicMock()
    mock_resources.ssh = MagicMock()
    mock_resources.playwright = MagicMock()
    mock_resources.vnc = MagicMock()
    return mock_resources


@patch("agents.services.agent_loop.AgentResourceManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.warm_up_model")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_task_complete(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_warm_up: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_resource_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent completes when DMR returns text (no tool calls)."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_resources = _make_mock_resources()
    mock_resource_cls.return_value.__enter__ = MagicMock(return_value=mock_resources)
    mock_resource_cls.return_value.__exit__ = MagicMock(return_value=False)

    # DMR returns a text response with no tool calls
    mock_send.return_value = DMRResponse(
        message=ChatMessage(
            role="assistant",
            content="Task completed successfully. I installed the software.",
        ),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=50,
    )

    dmr_config = DMRConfig(
        host="test",
        port="8080",
        model="test-model",
        temperature=0.0,
        max_tokens=1000,
    )
    agent_config = AgentConfig(
        dmr=dmr_config,
        max_iterations=10,
        timeout_seconds=300,
    )

    result = run_agent(
        "Install software",
        mock_ports,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.TASK_COMPLETE
    assert result.iterations == 1
    assert result.error is None
    assert len(result.messages) == 3  # system, user, assistant
    assert result.messages[2].role == "assistant"
    assert (
        result.messages[2].content
        == "Task completed successfully. I installed the software."
    )

    # Tool dispatch should not be called
    mock_dispatch.assert_not_called()


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=50000)
@patch("agents.services.agent_loop.AgentResourceManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.warm_up_model")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_with_tool_calls(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_warm_up: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_resource_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent executes tool calls, then completes."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_resources = _make_mock_resources()
    mock_resource_cls.return_value.__enter__ = MagicMock(return_value=mock_resources)
    mock_resource_cls.return_value.__exit__ = MagicMock(return_value=False)

    # First call: DMR returns a tool call
    tool_call = ToolCall(
        tool_call_id="tc1",
        tool_name="execute_command",
        arguments={"command": "apt-get install -y firefox"},
    )
    mock_send.side_effect = [
        DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="Let me install Firefox.",
                tool_calls=(tool_call,),
            ),
            finish_reason="tool_calls",
            usage_prompt_tokens=100,
            usage_completion_tokens=30,
        ),
        # Second call: DMR returns completion text
        DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="Firefox installed successfully.",
            ),
            finish_reason="stop",
            usage_prompt_tokens=150,
            usage_completion_tokens=20,
        ),
    ]

    # Mock the tool execution
    mock_dispatch.return_value = ToolResult(
        tool_call_id="tc1",
        content="Firefox installed",
        is_error=False,
    )

    dmr_config = DMRConfig(
        host="test",
        port="8080",
        model="test-model",
        temperature=0.0,
        max_tokens=1000,
    )
    agent_config = AgentConfig(
        dmr=dmr_config,
        max_iterations=10,
        timeout_seconds=300,
    )

    result = run_agent(
        "Install Firefox",
        mock_ports,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.TASK_COMPLETE
    assert result.iterations == 2
    assert result.error is None

    # Verify tool was dispatched with a ToolContext
    mock_dispatch.assert_called_once()
    call_args = mock_dispatch.call_args
    assert isinstance(call_args[0][1], ToolContext)

    # Verify message history
    # system, user, assistant (with tool call), tool result, assistant (completion)
    assert len(result.messages) == 5
    assert result.messages[2].role == "assistant"
    assert result.messages[2].tool_calls == (tool_call,)
    assert result.messages[3].role == "tool"
    assert result.messages[4].role == "assistant"
    assert result.messages[4].content == "Firefox installed successfully."


@patch("agents.services.agent_loop.AgentResourceManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.warm_up_model")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_max_iterations(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_warm_up: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_resource_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent stops at max_iterations."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_resources = _make_mock_resources()
    mock_resource_cls.return_value.__enter__ = MagicMock(return_value=mock_resources)
    mock_resource_cls.return_value.__exit__ = MagicMock(return_value=False)

    # Always return a tool call (never completes)
    tool_call = ToolCall(
        tool_call_id="tc1",
        tool_name="test_tool",
        arguments={"arg1": "value"},
    )
    mock_send.return_value = DMRResponse(
        message=ChatMessage(
            role="assistant",
            content="Working on it...",
            tool_calls=(tool_call,),
        ),
        finish_reason="tool_calls",
        usage_prompt_tokens=100,
        usage_completion_tokens=30,
    )

    mock_dispatch.return_value = ToolResult(
        tool_call_id="tc1",
        content="Result",
        is_error=False,
    )

    dmr_config = DMRConfig(
        host="test",
        port="8080",
        model="test-model",
        temperature=0.0,
        max_tokens=1000,
    )
    agent_config = AgentConfig(
        dmr=dmr_config,
        max_iterations=3,
        timeout_seconds=300,
    )

    result = run_agent(
        "Do something",
        mock_ports,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.MAX_ITERATIONS
    assert result.iterations == 3
    assert result.error is None

    # DMR should be called 3 times
    assert mock_send.call_count == 3
    # Tool should be dispatched 3 times
    assert mock_dispatch.call_count == 3


@patch("agents.services.agent_loop.AgentResourceManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.warm_up_model")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.time")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_timeout(
    mock_send: MagicMock,
    mock_get_tools: MagicMock,
    mock_time: MagicMock,
    mock_ensure_model: MagicMock,
    mock_warm_up: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_resource_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent stops on timeout."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_resources = _make_mock_resources()
    mock_resource_cls.return_value.__enter__ = MagicMock(return_value=mock_resources)
    mock_resource_cls.return_value.__exit__ = MagicMock(return_value=False)

    # Simulate time progression: start=0, first check=150, second check=400 (timeout)
    mock_time.monotonic.side_effect = [0, 150, 400]

    # DMR always returns tool calls
    tool_call = ToolCall(
        tool_call_id="tc1",
        tool_name="test_tool",
        arguments={"arg1": "value"},
    )
    mock_send.return_value = DMRResponse(
        message=ChatMessage(
            role="assistant",
            content="Working...",
            tool_calls=(tool_call,),
        ),
        finish_reason="tool_calls",
        usage_prompt_tokens=100,
        usage_completion_tokens=30,
    )

    dmr_config = DMRConfig(
        host="test",
        port="8080",
        model="test-model",
        temperature=0.0,
        max_tokens=1000,
    )
    agent_config = AgentConfig(
        dmr=dmr_config,
        max_iterations=100,
        timeout_seconds=300,  # 300 second timeout
    )

    result = run_agent(
        "Long task",
        mock_ports,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.TIMEOUT
    assert result.iterations == 1  # Only completed 1 iteration before timeout
    assert result.error is not None
    assert "Timed out" in result.error
    assert "400" in result.error  # Should show elapsed time


@patch("agents.services.agent_loop.AgentResourceManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.warm_up_model")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_dmr_error(
    mock_send: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_warm_up: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_resource_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent handles DMR errors gracefully."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_resources = _make_mock_resources()
    mock_resource_cls.return_value.__enter__ = MagicMock(return_value=mock_resources)
    mock_resource_cls.return_value.__exit__ = MagicMock(return_value=False)

    # DMR raises an exception
    mock_send.side_effect = ConnectionError("DMR service unavailable")

    dmr_config = DMRConfig(
        host="test",
        port="8080",
        model="test-model",
        temperature=0.0,
        max_tokens=1000,
    )
    agent_config = AgentConfig(
        dmr=dmr_config,
        max_iterations=10,
        timeout_seconds=300,
    )

    result = run_agent(
        "Test task",
        mock_ports,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.ERROR
    assert result.iterations == 1
    assert result.error is not None
    assert "DMR request failed" in result.error
    assert "DMR service unavailable" in result.error


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=50000)
def test_build_tool_result_message() -> None:
    """Test that _build_tool_result_message creates correct ChatMessage."""
    tool_result = ToolResult(
        tool_call_id="tc123",
        content="Command executed successfully",
        is_error=False,
    )

    message = _build_tool_result_message(tool_result)

    assert message.role == "tool"
    assert message.content == "Command executed successfully"
    assert message.tool_call_id == "tc123"
    assert message.tool_calls is None


@patch("agents.services.agent_loop.AgentResourceManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.warm_up_model")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_creates_resource_manager(
    mock_send: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_warm_up: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_resource_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent creates an AgentResourceManager as context manager."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_resources = _make_mock_resources()
    mock_resource_cls.return_value.__enter__ = MagicMock(return_value=mock_resources)
    mock_resource_cls.return_value.__exit__ = MagicMock(return_value=False)

    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="Done."),
        finish_reason="stop",
        usage_prompt_tokens=50,
        usage_completion_tokens=10,
    )

    dmr_config = DMRConfig(
        host="test", port="8080", model="m", temperature=0.0, max_tokens=100
    )
    agent_config = AgentConfig(dmr=dmr_config, max_iterations=5, timeout_seconds=60)

    run_agent("test", mock_ports, config=agent_config)

    mock_resource_cls.assert_called_once_with(mock_ports)
    mock_resource_cls.return_value.__enter__.assert_called_once()
    mock_resource_cls.return_value.__exit__.assert_called_once()
