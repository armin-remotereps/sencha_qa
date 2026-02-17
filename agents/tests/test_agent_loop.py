from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from agents.services.agent_loop import (
    _build_environment_context,
    _build_qa_rules,
    _build_role_description,
    _build_task_section,
    _build_tool_guidelines,
    _build_tool_result_message,
    _get_os_name,
    _run_agent_loop,
    build_agent_config,
    build_system_prompt,
    run_agent,
)
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


@pytest.fixture
def mock_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Fixture for mock tool definitions."""
    return (
        ToolDefinition(
            name="test_tool",
            description="A test tool",
            category=ToolCategory.CONTROLLER,
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
    """Test that build_system_prompt includes the task description for Linux."""
    task = "Install Firefox and open Google"
    prompt = build_system_prompt(task, system_info={"os": "Linux"})

    assert "Install Firefox and open Google" in prompt
    assert "strict QA tester" in prompt
    assert "Linux desktop environment" in prompt
    assert "Ubuntu 24.04 with XFCE4" in prompt
    assert "execute_command" in prompt
    assert "take_screenshot" in prompt
    assert "click" in prompt
    assert "type_text" in prompt
    assert "key_press" in prompt
    assert "hover" in prompt
    assert "drag" in prompt
    assert "vision-based" in prompt or "AI-based" in prompt
    assert "browser_navigate" in prompt
    assert "Chromium" in prompt
    assert "whoami" in prompt
    assert "Preconditions" in prompt
    assert "installation wizards" in prompt
    assert "browser_download" in prompt


def test_build_system_prompt_vision_tools() -> None:
    """Test that build_system_prompt includes vision-based tool examples."""
    prompt = build_system_prompt("test task", system_info=None)

    assert "vision" in prompt.lower()
    assert "click" in prompt
    assert "hover" in prompt
    assert "drag" in prompt
    assert "take_screenshot" in prompt
    assert "description" in prompt


def test_build_system_prompt_macos() -> None:
    """Test that macOS system_info produces macOS-specific prompt."""
    prompt = build_system_prompt("test task", system_info={"os": "Darwin"})

    assert "macOS" in prompt
    assert "brew" in prompt
    assert "/Applications/" in prompt
    assert "XFCE4" not in prompt
    assert "apt-get" not in prompt
    assert "Ubuntu" not in prompt
    assert "full desktop access" in prompt
    assert "Finder" in prompt
    assert "browser to download" in prompt


def test_build_system_prompt_windows() -> None:
    """Test that Windows system_info produces Windows-specific prompt."""
    prompt = build_system_prompt("test task", system_info={"os": "Windows"})

    assert "Windows" in prompt
    assert "winget" in prompt
    assert "XFCE4" not in prompt
    assert "apt-get" not in prompt
    assert "full desktop access" in prompt
    assert "File Explorer" in prompt


def test_build_system_prompt_no_system_info() -> None:
    """Test that None system_info falls back to Linux."""
    prompt = build_system_prompt("test task", system_info=None)

    assert "Linux desktop environment" in prompt
    assert "XFCE4" in prompt


def test_build_system_prompt_empty_dict() -> None:
    """Test that empty dict system_info falls back to Linux."""
    prompt = build_system_prompt("test task", system_info={})

    assert "Linux desktop environment" in prompt
    assert "XFCE4" in prompt


def test_get_os_name_variants() -> None:
    """Test _get_os_name with various inputs."""
    assert _get_os_name(None) == "Linux"
    assert _get_os_name({}) == "Linux"
    assert _get_os_name({"os": "Darwin"}) == "Darwin"
    assert _get_os_name({"os": "Windows"}) == "Windows"
    assert _get_os_name({"os": "Linux"}) == "Linux"


def test_build_environment_context_darwin() -> None:
    """Test that Darwin environment context mentions macOS specifics."""
    ctx = _build_environment_context(system_info={"os": "Darwin"})
    assert "macOS" in ctx
    assert "brew" in ctx
    assert "/Applications/" in ctx


def test_build_environment_context_windows() -> None:
    """Test that Windows environment context mentions Windows specifics."""
    ctx = _build_environment_context(system_info={"os": "Windows"})
    assert "Windows" in ctx
    assert "winget" in ctx


def test_build_environment_context_linux_fallback() -> None:
    """Test that Linux/default environment context uses XFCE4."""
    ctx = _build_environment_context(system_info=None)
    assert "XFCE4" in ctx
    assert "display :0" in ctx


@override_settings(
    AGENT_MAX_ITERATIONS=50,
    AGENT_TIMEOUT_SECONDS=600,
    DMR_HOST="test-dmr",
    DMR_PORT="8080",
    DMR_MODEL="test-model",
    DMR_VISION_MODEL="test-vision-model",
    DMR_TEMPERATURE=0.7,
    DMR_MAX_TOKENS=4096,
    VISION_BACKEND="dmr",
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
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent completes when DMR returns text (no tool calls)."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None

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
        project_id=1,
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
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent executes tool calls, then completes."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None

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
        project_id=1,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.TASK_COMPLETE
    assert result.iterations == 2
    assert result.error is None

    # Verify tool was dispatched with a ToolContext
    mock_dispatch.assert_called_once()
    call_args = mock_dispatch.call_args
    assert isinstance(call_args[0][1], ToolContext)
    assert call_args[0][1].project_id == 1

    # Verify message history
    # system, user, assistant (with tool call), tool result, assistant (completion)
    assert len(result.messages) == 5
    assert result.messages[2].role == "assistant"
    assert result.messages[2].tool_calls == (tool_call,)
    assert result.messages[3].role == "tool"
    assert result.messages[4].role == "assistant"
    assert result.messages[4].content == "Firefox installed successfully."


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
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent stops at max_iterations."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None

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
        project_id=1,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.MAX_ITERATIONS
    assert result.iterations == 3
    assert result.error is None

    # DMR should be called 3 times
    assert mock_send.call_count == 3
    # Tool should be dispatched 3 times
    assert mock_dispatch.call_count == 3


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
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent stops on timeout."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None

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
        project_id=1,
        config=agent_config,
    )

    assert result.stop_reason == AgentStopReason.TIMEOUT
    assert result.iterations == 1  # Only completed 1 iteration before timeout
    assert result.error is not None
    assert "Timed out" in result.error
    assert "400" in result.error  # Should show elapsed time


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
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Test that run_agent handles DMR errors gracefully."""
    mock_get_tools.return_value = mock_tool_definitions
    mock_build_summarizer.return_value = None

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
        project_id=1,
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


@override_settings(
    OUTPUT_SUMMARIZE_THRESHOLD=50000,
    CONTEXT_SUMMARIZE_THRESHOLD=50000,
    CONTEXT_PRESERVE_LAST_MESSAGES=6,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=8000,
)
@patch("agents.services.agent_loop.summarize_context_if_needed")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.dispatch_tool_call")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_calls_context_summarizer(
    mock_send: MagicMock,
    mock_dispatch: MagicMock,
    mock_get_tools: MagicMock,
    mock_summarize_ctx: MagicMock,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Verify summarize_context_if_needed is called each iteration."""
    mock_get_tools.return_value = mock_tool_definitions
    # Pass through unchanged
    mock_summarize_ctx.side_effect = lambda msgs, **kw: msgs

    tool_call = ToolCall(
        tool_call_id="tc1",
        tool_name="test_tool",
        arguments={"arg1": "val"},
    )
    mock_send.side_effect = [
        DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="step 1",
                tool_calls=(tool_call,),
            ),
            finish_reason="tool_calls",
            usage_prompt_tokens=50,
            usage_completion_tokens=10,
        ),
        DMRResponse(
            message=ChatMessage(role="assistant", content="Done."),
            finish_reason="stop",
            usage_prompt_tokens=50,
            usage_completion_tokens=10,
        ),
    ]
    mock_dispatch.return_value = ToolResult(
        tool_call_id="tc1", content="ok", is_error=False
    )

    mock_context = MagicMock(spec=ToolContext)
    mock_context.summarizer_config = None

    dmr_config = DMRConfig(
        host="test", port="8080", model="m", temperature=0.0, max_tokens=100
    )
    agent_config = AgentConfig(dmr=dmr_config, max_iterations=5, timeout_seconds=60)

    _run_agent_loop("test", mock_context, config=agent_config)

    # Should be called once per iteration (2 iterations)
    assert mock_summarize_ctx.call_count == 2


@override_settings(
    OUTPUT_SUMMARIZE_THRESHOLD=50000,
    CONTEXT_SUMMARIZE_THRESHOLD=50000,
    CONTEXT_PRESERVE_LAST_MESSAGES=6,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=8000,
)
@patch("agents.services.agent_loop.summarize_context_if_needed")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_run_agent_uses_summarized_messages(
    mock_send: MagicMock,
    mock_get_tools: MagicMock,
    mock_summarize_ctx: MagicMock,
    mock_tool_definitions: tuple[ToolDefinition, ...],
) -> None:
    """Verify DMR receives the summarized message list."""
    mock_get_tools.return_value = mock_tool_definitions

    # Summarizer replaces messages with a shorter list
    short_messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="summary"),
    ]
    mock_summarize_ctx.return_value = short_messages

    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="Done."),
        finish_reason="stop",
        usage_prompt_tokens=50,
        usage_completion_tokens=10,
    )

    mock_context = MagicMock(spec=ToolContext)
    mock_context.summarizer_config = None

    dmr_config = DMRConfig(
        host="test", port="8080", model="m", temperature=0.0, max_tokens=100
    )
    agent_config = AgentConfig(dmr=dmr_config, max_iterations=5, timeout_seconds=60)

    _run_agent_loop("test", mock_context, config=agent_config)

    # DMR should receive the short list (as tuple)
    call_args = mock_send.call_args
    sent_messages = call_args[0][1]
    assert len(sent_messages) == 2
    assert sent_messages[1].content == "summary"


@patch("agents.services.agent_loop.summarize_context_if_needed")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_on_log_callback_fires_on_task_complete(
    mock_send: MagicMock,
    mock_tools: MagicMock,
    mock_summarize_ctx: MagicMock,
) -> None:
    """Test that on_log callback is called during agent loop."""
    mock_tools.return_value = ()
    mock_summarize_ctx.side_effect = lambda msgs, **kw: msgs
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="Done"),
        finish_reason="stop",
        usage_prompt_tokens=10,
        usage_completion_tokens=5,
    )

    log_messages: list[str] = []
    config = AgentConfig(
        dmr=DMRConfig(host="localhost", port="12434", model="test"),
        max_iterations=5,
        on_log=log_messages.append,
    )

    context = ToolContext(
        project_id=1,
    )

    result = _run_agent_loop("Test task", context, config=config)

    assert result.stop_reason == AgentStopReason.TASK_COMPLETE
    assert len(log_messages) >= 2  # At least iteration + completion messages
    assert any("Agent iteration" in msg for msg in log_messages)
    assert any("Agent completed" in msg for msg in log_messages)


@patch("agents.services.agent_loop.summarize_context_if_needed")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
def test_on_log_callback_not_called_when_none(
    mock_send: MagicMock,
    mock_tools: MagicMock,
    mock_summarize_ctx: MagicMock,
) -> None:
    """Test that no error when on_log is None."""
    mock_tools.return_value = ()
    mock_summarize_ctx.side_effect = lambda msgs, **kw: msgs
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="Done"),
        finish_reason="stop",
        usage_prompt_tokens=10,
        usage_completion_tokens=5,
    )

    config = AgentConfig(
        dmr=DMRConfig(host="localhost", port="12434", model="test"),
        max_iterations=5,
    )

    context = ToolContext(
        project_id=1,
    )

    result = _run_agent_loop("Test task", context, config=config)
    assert result.stop_reason == AgentStopReason.TASK_COMPLETE


@patch("agents.services.agent_loop.summarize_context_if_needed")
@patch("agents.services.agent_loop.get_all_tool_definitions")
@patch("agents.services.agent_loop.send_chat_completion")
@patch("agents.services.agent_loop.dispatch_tool_call")
def test_on_log_callback_fires_on_tool_calls(
    mock_dispatch: MagicMock,
    mock_send: MagicMock,
    mock_tools: MagicMock,
    mock_summarize_ctx: MagicMock,
) -> None:
    """Test that on_log fires for tool call and tool result messages."""
    mock_tools.return_value = ()
    mock_summarize_ctx.side_effect = lambda msgs, **kw: msgs
    mock_dispatch.return_value = ToolResult(
        tool_call_id="call_1", content="tool output", is_error=False
    )

    # First call: tool call, Second call: task complete
    mock_send.side_effect = [
        DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="Let me use a tool",
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_1",
                        tool_name="test_tool",
                        arguments={"arg": "val"},
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage_prompt_tokens=10,
            usage_completion_tokens=5,
        ),
        DMRResponse(
            message=ChatMessage(role="assistant", content="All done"),
            finish_reason="stop",
            usage_prompt_tokens=10,
            usage_completion_tokens=5,
        ),
    ]

    log_messages: list[str] = []
    config = AgentConfig(
        dmr=DMRConfig(host="localhost", port="12434", model="test"),
        max_iterations=5,
        on_log=log_messages.append,
    )

    context = ToolContext(
        project_id=1,
    )

    result = _run_agent_loop("Test task", context, config=config)

    assert result.stop_reason == AgentStopReason.TASK_COMPLETE
    assert any("[Tool Call]" in msg for msg in log_messages)
    assert any("[Tool Result]" in msg for msg in log_messages)
    assert any("[Agent]" in msg for msg in log_messages)


def test_build_qa_rules_preconditions() -> None:
    """Test that QA rules include mandatory preconditions handling."""
    rules = _build_qa_rules()

    assert "Preconditions" in rules
    assert "FIRST" in rules
    assert "mandatory setup" in rules


def test_build_tool_guidelines_download_via_browser() -> None:
    """Test that tool guidelines include browser-based download strategy."""
    guidelines = _build_tool_guidelines()

    assert "downloading" in guidelines
    assert "browser_navigate" in guidelines
    assert "installation wizards" in guidelines


def test_build_tool_guidelines_browser_download() -> None:
    """Test that tool guidelines include browser_download tool."""
    guidelines = _build_tool_guidelines()

    assert "browser_download" in guidelines
    assert "direct URL" in guidelines or "direct download" in guidelines


def test_build_tool_guidelines_desktop_fallback() -> None:
    """Test that tool guidelines include numbered desktop fallback escalation."""
    guidelines = _build_tool_guidelines()

    assert "DESKTOP FALLBACK" in guidelines
    assert "2 attempts" in guidelines
    assert "ANY reason" in guidelines
    assert "take_screenshot" in guidelines
    assert "1." in guidelines
    assert "2." in guidelines
    assert "3." in guidelines
    assert "4." in guidelines


def test_build_tool_guidelines_web_search() -> None:
    """Test that tool guidelines include web_search and installation lookup."""
    guidelines = _build_tool_guidelines()

    assert "web_search" in guidelines
    assert "INSTALLATION LOOKUP" in guidelines


def test_build_qa_rules_authentication_failfast() -> None:
    """Test that QA rules include authentication fail-fast rule."""
    rules = _build_qa_rules()

    assert "AUTHENTICATION" in rules
    assert "credentials" in rules
    assert "FAIL" in rules
    assert "Do NOT search" in rules


def test_build_tool_guidelines_retry_limits() -> None:
    """Test that tool guidelines include retry limits section."""
    guidelines = _build_tool_guidelines()

    assert "RETRY LIMITS" in guidelines
    assert "3 times" in guidelines
    assert "STOP" in guidelines


def test_build_system_prompt_includes_search_tools() -> None:
    """Test that system prompt includes search tool taxonomy."""
    prompt = build_system_prompt("test task", system_info=None)

    assert "SEARCH TOOLS" in prompt
    assert "web_search" in prompt
