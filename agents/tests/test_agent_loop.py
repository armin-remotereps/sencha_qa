from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from agents.services.agent_loop import (
    _build_tool_result_message,
    build_agent_config,
    build_system_prompt,
    describe_screenshot,
    run_agent,
)
from agents.types import (
    AgentConfig,
    AgentStopReason,
    ChatMessage,
    DMRConfig,
    DMRResponse,
    ImageContent,
    TextContent,
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
    assert "Ubuntu 24.04 with XFCE4" in prompt


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


@patch("agents.services.agent_loop.SSHSessionManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_task_complete(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_ssh_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent completes when DMR returns text (no tool calls)."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_session = MagicMock()
    mock_ssh_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_ssh_cls.return_value.__exit__ = MagicMock(return_value=False)

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
@patch("agents.services.agent_loop.SSHSessionManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_with_tool_calls(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_ssh_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent executes tool calls, then completes."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_session = MagicMock()
    mock_ssh_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_ssh_cls.return_value.__exit__ = MagicMock(return_value=False)

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


@patch("agents.services.agent_loop.SSHSessionManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_max_iterations(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_ssh_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent stops at max_iterations."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_session = MagicMock()
    mock_ssh_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_ssh_cls.return_value.__exit__ = MagicMock(return_value=False)

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


@patch("agents.services.agent_loop.SSHSessionManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.time")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_timeout(
    mock_send: MagicMock,
    mock_get_tools: MagicMock,
    mock_time: MagicMock,
    mock_ensure_model: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_ssh_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent stops on timeout."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_session = MagicMock()
    mock_ssh_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_ssh_cls.return_value.__exit__ = MagicMock(return_value=False)

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


@patch("agents.services.agent_loop.SSHSessionManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_dmr_error(
    mock_send: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_ssh_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent handles DMR errors gracefully."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_session = MagicMock()
    mock_ssh_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_ssh_cls.return_value.__exit__ = MagicMock(return_value=False)

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
@patch("agents.services.agent_loop.SSHSessionManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_screenshot_without_vision_model(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_ssh_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that screenshots fall back to raw image when vision_dmr is None."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_session = MagicMock()
    mock_ssh_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_ssh_cls.return_value.__exit__ = MagicMock(return_value=False)

    tool_call = ToolCall(
        tool_call_id="tc1",
        tool_name="take_screenshot",
        arguments={},
    )
    mock_send.side_effect = [
        DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="Taking screenshot...",
                tool_calls=(tool_call,),
            ),
            finish_reason="tool_calls",
            usage_prompt_tokens=100,
            usage_completion_tokens=30,
        ),
        DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="I can see the desktop. Task complete.",
            ),
            finish_reason="stop",
            usage_prompt_tokens=200,
            usage_completion_tokens=40,
        ),
    ]

    mock_dispatch.return_value = ToolResult(
        tool_call_id="tc1",
        content="Screenshot taken",
        is_error=False,
        image_base64="iVBORw0KGgoAAAANS",
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
        vision_dmr=None,
        max_iterations=10,
        timeout_seconds=300,
    )

    result = run_agent(
        "Take a screenshot",
        mock_ports,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.TASK_COMPLETE
    assert result.iterations == 2

    # system, user, assistant (tool call), tool result, user (image), assistant
    assert len(result.messages) == 6
    assert result.messages[4].role == "user"

    content = result.messages[4].content
    assert isinstance(content, tuple)
    assert len(content) == 2
    assert isinstance(content[0], TextContent)
    assert content[0].text == "Here is the screenshot:"
    assert isinstance(content[1], ImageContent)
    assert content[1].base64_data == "iVBORw0KGgoAAAANS"


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=50000)
@patch("agents.services.agent_loop.describe_screenshot")
@patch("agents.services.agent_loop.SSHSessionManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_screenshot_with_vision_model(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_ssh_cls: MagicMock,
    mock_describe: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that screenshots use vision model when vision_dmr is set."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_session = MagicMock()
    mock_ssh_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_ssh_cls.return_value.__exit__ = MagicMock(return_value=False)
    mock_describe.return_value = "A desktop with a terminal window open."

    tool_call = ToolCall(
        tool_call_id="tc1",
        tool_name="take_screenshot",
        arguments={},
    )
    mock_send.side_effect = [
        DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="Taking screenshot...",
                tool_calls=(tool_call,),
            ),
            finish_reason="tool_calls",
            usage_prompt_tokens=100,
            usage_completion_tokens=30,
        ),
        DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="I see a terminal. Task complete.",
            ),
            finish_reason="stop",
            usage_prompt_tokens=200,
            usage_completion_tokens=40,
        ),
    ]

    mock_dispatch.return_value = ToolResult(
        tool_call_id="tc1",
        content="Screenshot taken",
        is_error=False,
        image_base64="iVBORw0KGgoAAAANS",
    )

    dmr_config = DMRConfig(
        host="test",
        port="8080",
        model="test-action-model",
        temperature=0.0,
        max_tokens=1000,
    )
    vision_config = DMRConfig(
        host="test",
        port="8080",
        model="test-vision-model",
        temperature=0.0,
        max_tokens=1000,
    )
    agent_config = AgentConfig(
        dmr=dmr_config,
        vision_dmr=vision_config,
        max_iterations=10,
        timeout_seconds=300,
    )

    result = run_agent(
        "Take a screenshot",
        mock_ports,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.TASK_COMPLETE
    assert result.iterations == 2

    # describe_screenshot should have been called with vision config and image
    mock_describe.assert_called_once_with(vision_config, "iVBORw0KGgoAAAANS")

    # system, user, assistant (tool call), tool result, user (description), assistant
    assert len(result.messages) == 6
    assert result.messages[4].role == "user"

    # The user message should be a text description, not a raw image
    content = result.messages[4].content
    assert isinstance(content, str)
    assert "[Screenshot description]" in content
    assert "A desktop with a terminal window open." in content


@patch("agents.services.agent_loop.send_chat_completion")
def test_describe_screenshot(mock_send: MagicMock) -> None:
    """Test describe_screenshot sends image to vision model and returns text."""
    mock_send.return_value = DMRResponse(
        message=ChatMessage(
            role="assistant",
            content="The screenshot shows a desktop with XFCE panel.",
        ),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=50,
    )

    vision_config = DMRConfig(
        host="test",
        port="8080",
        model="test-vision",
        temperature=0.0,
        max_tokens=1000,
    )
    result = describe_screenshot(vision_config, "iVBORw0KGgoAAAANS")

    assert result == "The screenshot shows a desktop with XFCE panel."
    mock_send.assert_called_once()

    # Verify the messages sent to the vision model
    call_args = mock_send.call_args
    messages = call_args[0][1]
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    assert isinstance(messages[1].content, tuple)
    assert isinstance(messages[1].content[0], TextContent)
    assert isinstance(messages[1].content[1], ImageContent)
    assert messages[1].content[1].base64_data == "iVBORw0KGgoAAAANS"


@patch("agents.services.agent_loop.send_chat_completion")
def test_describe_screenshot_non_string_response(mock_send: MagicMock) -> None:
    """Test describe_screenshot returns fallback when response is not a string."""
    mock_send.return_value = DMRResponse(
        message=ChatMessage(
            role="assistant",
            content=None,
        ),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=0,
    )

    vision_config = DMRConfig(
        host="test",
        port="8080",
        model="test-vision",
        temperature=0.0,
        max_tokens=1000,
    )
    result = describe_screenshot(vision_config, "iVBORw0KGgoAAAANS")

    assert result == "Unable to describe screenshot."


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


@patch("agents.services.agent_loop.SSHSessionManager")
@patch("agents.services.agent_loop.build_summarizer_config")
@patch("agents.services.agent_loop.ensure_model_available")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_creates_ssh_session(
    mock_send: MagicMock,
    mock_get_tools: MagicMock,
    mock_ensure_model: MagicMock,
    mock_build_summarizer: MagicMock,
    mock_ssh_cls: MagicMock,
    mock_ports: ContainerPorts,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent creates an SSHSessionManager as context manager."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None
    mock_session = MagicMock()
    mock_ssh_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_ssh_cls.return_value.__exit__ = MagicMock(return_value=False)

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

    # SSHSessionManager should have been instantiated with ports
    mock_ssh_cls.assert_called_once_with(mock_ports)
    # __enter__ and __exit__ should have been called (context manager protocol)
    mock_ssh_cls.return_value.__enter__.assert_called_once()
    mock_ssh_cls.return_value.__exit__.assert_called_once()
