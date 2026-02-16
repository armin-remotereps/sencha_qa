from __future__ import annotations

import pytest

from agents.types import (
    AgentConfig,
    AgentResult,
    AgentStopReason,
    ChatMessage,
    ContentPart,
    DMRConfig,
    DMRResponse,
    ImageContent,
    TextContent,
    ToolCall,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolResult,
)


def test_tool_category_enum_members() -> None:
    """Test ToolCategory enum has all expected members."""
    assert ToolCategory.CONTROLLER.value == "controller"
    assert ToolCategory.BROWSER.value == "browser"
    assert ToolCategory.SEARCH.value == "search"
    assert len(list(ToolCategory)) == 3


def test_agent_stop_reason_enum_members() -> None:
    """Test AgentStopReason enum has all expected members."""
    assert AgentStopReason.MAX_ITERATIONS.value == "max_iterations"
    assert AgentStopReason.TIMEOUT.value == "timeout"
    assert AgentStopReason.TASK_COMPLETE.value == "task_complete"
    assert AgentStopReason.ERROR.value == "error"
    assert len(list(AgentStopReason)) == 4


def test_tool_parameter_is_frozen() -> None:
    """Test ToolParameter dataclass is frozen and immutable."""
    param = ToolParameter(
        name="test", type="string", description="Test param", required=True
    )
    with pytest.raises(Exception):  # FrozenInstanceError in Python 3.11+
        param.name = "changed"  # type: ignore[misc]


def test_tool_definition_is_frozen() -> None:
    """Test ToolDefinition dataclass is frozen and immutable."""
    param = ToolParameter(
        name="cmd", type="string", description="Command", required=True
    )
    tool = ToolDefinition(
        name="shell",
        description="Run shell command",
        category=ToolCategory.CONTROLLER,
        parameters=(param,),
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        tool.name = "changed"  # type: ignore[misc]


def test_tool_parameter_with_enum() -> None:
    """Test ToolParameter with enum values."""
    param = ToolParameter(
        name="level",
        type="string",
        description="Log level",
        required=False,
        enum=("INFO", "DEBUG", "ERROR"),
    )
    assert param.enum == ("INFO", "DEBUG", "ERROR")
    assert param.required is False


def test_content_part_union_with_text() -> None:
    """Test ContentPart union works with TextContent."""
    text = TextContent(text="Hello world")
    parts: tuple[ContentPart, ...] = (text,)
    assert isinstance(parts[0], TextContent)
    assert parts[0].text == "Hello world"


def test_content_part_union_with_image() -> None:
    """Test ContentPart union works with ImageContent."""
    image = ImageContent(base64_data="abc123", media_type="image/jpeg")
    parts: tuple[ContentPart, ...] = (image,)
    assert isinstance(parts[0], ImageContent)
    assert parts[0].base64_data == "abc123"
    assert parts[0].media_type == "image/jpeg"


def test_content_part_union_with_mixed() -> None:
    """Test ContentPart union works with mixed text and image."""
    text = TextContent(text="Screenshot:")
    image = ImageContent(base64_data="xyz789")
    parts: tuple[ContentPart, ...] = (text, image)
    assert len(parts) == 2
    assert isinstance(parts[0], TextContent)
    assert isinstance(parts[1], ImageContent)


def test_chat_message_with_tool_calls() -> None:
    """Test ChatMessage with tool_calls."""
    tool_call = ToolCall(
        tool_call_id="call_123", tool_name="shell", arguments={"cmd": "ls"}
    )
    msg = ChatMessage(
        role="assistant",
        content="I will run a command",
        tool_calls=(tool_call,),
    )
    assert msg.tool_calls is not None
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].tool_name == "shell"


def test_chat_message_as_tool_result() -> None:
    """Test ChatMessage as a tool result (role='tool')."""
    msg = ChatMessage(
        role="tool",
        content="Command output: success",
        tool_call_id="call_123",
    )
    assert msg.role == "tool"
    assert msg.tool_call_id == "call_123"
    assert msg.content == "Command output: success"


def test_dmr_config_defaults() -> None:
    """Test DMRConfig default values."""
    config = DMRConfig(host="localhost", port="8080", model="llama-3")
    assert config.temperature == 0.9
    assert config.max_tokens == 4096


def test_dmr_config_custom_values() -> None:
    """Test DMRConfig with custom temperature and max_tokens."""
    config = DMRConfig(
        host="192.168.1.1",
        port="9000",
        model="gpt-4",
        temperature=0.7,
        max_tokens=2048,
    )
    assert config.host == "192.168.1.1"
    assert config.temperature == 0.7
    assert config.max_tokens == 2048


def test_agent_config_defaults() -> None:
    """Test AgentConfig default values."""
    dmr = DMRConfig(host="localhost", port="8080", model="llama-3")
    config = AgentConfig(dmr=dmr)
    assert config.max_iterations == 30
    assert config.timeout_seconds == 900
    assert config.on_screenshot is None


def test_agent_result_is_frozen() -> None:
    """Test AgentResult dataclass is frozen."""
    msg = ChatMessage(role="user", content="test")
    result = AgentResult(
        stop_reason=AgentStopReason.TASK_COMPLETE,
        iterations=5,
        messages=(msg,),
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        result.iterations = 10  # type: ignore[misc]


def test_tool_result_with_error() -> None:
    """Test ToolResult with is_error=True."""
    result = ToolResult(
        tool_call_id="call_456",
        content="Command failed: permission denied",
        is_error=True,
    )
    assert result.is_error is True


def test_tool_result_fields() -> None:
    """Test ToolResult has correct fields."""
    result = ToolResult(
        tool_call_id="call_789",
        content="Screenshot captured",
        is_error=False,
    )
    assert result.is_error is False
    assert result.content == "Screenshot captured"
    assert result.tool_call_id == "call_789"


def test_image_content_default_media_type() -> None:
    """Test ImageContent default media_type is image/png."""
    image = ImageContent(base64_data="abc")
    assert image.media_type == "image/png"
