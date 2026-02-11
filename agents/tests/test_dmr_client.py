from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from agents.services.dmr_client import send_chat_completion
from agents.services.dmr_config import build_dmr_config, build_vision_dmr_config
from agents.services.dmr_model_manager import (
    _normalize_model_id,
    ensure_model_available,
    is_model_available,
    list_models,
    warm_up_model,
)
from agents.services.dmr_serializer import (
    _parse_response,
    _parse_tool_calls,
    _serialize_content,
    _serialize_messages,
    _serialize_single_message,
    _serialize_tools,
)
from agents.types import (
    ChatMessage,
    DMRConfig,
    ImageContent,
    TextContent,
    ToolCall,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
)


@override_settings(
    DMR_HOST="test-host",
    DMR_PORT="8080",
    DMR_MODEL="test-model",
    DMR_TEMPERATURE=0.5,
    DMR_MAX_TOKENS=2048,
)
def test_build_dmr_config_from_settings() -> None:
    """Test build_dmr_config reads from Django settings."""
    config = build_dmr_config()
    assert config.host == "test-host"
    assert config.port == "8080"
    assert config.model == "test-model"
    assert config.temperature == 0.5
    assert config.max_tokens == 2048


@override_settings(
    DMR_HOST="localhost",
    DMR_PORT="9000",
    DMR_MODEL="default-model",
    DMR_TEMPERATURE=0.1,
    DMR_MAX_TOKENS=4096,
)
def test_build_dmr_config_with_model_override() -> None:
    """Test build_dmr_config with model override."""
    config = build_dmr_config(model="custom-model")
    assert config.host == "localhost"
    assert config.port == "9000"
    assert config.model == "custom-model"  # Overridden
    assert config.temperature == 0.1
    assert config.max_tokens == 4096


def test_serialize_messages_with_plain_text() -> None:
    """Test _serialize_messages with plain text messages."""
    messages = (
        ChatMessage(role="system", content="You are a helpful assistant."),
        ChatMessage(role="user", content="Hello!"),
        ChatMessage(role="assistant", content="Hi there!"),
    )
    serialized = _serialize_messages(messages)
    assert len(serialized) == 3
    assert serialized[0] == {
        "role": "system",
        "content": "You are a helpful assistant.",
    }
    assert serialized[1] == {"role": "user", "content": "Hello!"}
    assert serialized[2] == {"role": "assistant", "content": "Hi there!"}


def test_serialize_single_message_with_tool_calls() -> None:
    """Test _serialize_single_message with tool_calls."""
    tool_call = ToolCall(
        tool_call_id="call_abc123",
        tool_name="shell",
        arguments={"cmd": "ls -la"},
    )
    message = ChatMessage(
        role="assistant",
        content="I will list files",
        tool_calls=(tool_call,),
    )
    serialized = _serialize_single_message(message)
    assert serialized["role"] == "assistant"
    assert serialized["content"] == "I will list files"
    assert "tool_calls" in serialized
    tool_calls_list = serialized["tool_calls"]
    assert isinstance(tool_calls_list, list)
    assert len(tool_calls_list) == 1
    assert tool_calls_list[0]["id"] == "call_abc123"
    assert tool_calls_list[0]["type"] == "function"
    assert tool_calls_list[0]["function"]["name"] == "shell"
    assert json.loads(tool_calls_list[0]["function"]["arguments"]) == {"cmd": "ls -la"}


def test_serialize_single_message_with_tool_call_id() -> None:
    """Test _serialize_single_message as a tool response."""
    message = ChatMessage(
        role="tool",
        content="Files listed successfully",
        tool_call_id="call_abc123",
    )
    serialized = _serialize_single_message(message)
    assert serialized["role"] == "tool"
    assert serialized["content"] == "Files listed successfully"
    assert serialized["tool_call_id"] == "call_abc123"


def test_serialize_content_multimodal_with_image() -> None:
    """Test _serialize_content with multimodal (text + image) content."""
    text_part = TextContent(text="Here is a screenshot:")
    image_part = ImageContent(base64_data="iVBORw0KGgoAAAANS", media_type="image/png")
    content: tuple[TextContent | ImageContent, ...] = (text_part, image_part)
    serialized = _serialize_content(content)
    assert isinstance(serialized, list)
    assert len(serialized) == 2
    assert serialized[0] == {"type": "text", "text": "Here is a screenshot:"}
    assert serialized[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANS"},
    }


def test_serialize_content_plain_string() -> None:
    """Test _serialize_content with plain string content."""
    serialized = _serialize_content("Just a plain string")
    assert serialized == "Just a plain string"


def test_serialize_tools() -> None:
    """Test _serialize_tools serializes ToolDefinitions correctly."""
    param1 = ToolParameter(
        name="command", type="string", description="Shell command", required=True
    )
    param2 = ToolParameter(
        name="workdir",
        type="string",
        description="Working directory",
        required=False,
    )
    param3 = ToolParameter(
        name="level",
        type="string",
        description="Log level",
        required=True,
        enum=("DEBUG", "INFO", "ERROR"),
    )
    tool = ToolDefinition(
        name="run_shell",
        description="Execute a shell command",
        category=ToolCategory.SHELL,
        parameters=(param1, param2, param3),
    )
    serialized = _serialize_tools((tool,))
    assert len(serialized) == 1
    func_schema: dict[str, object] = serialized[0]
    assert func_schema["type"] == "function"
    function_obj = func_schema["function"]
    assert isinstance(function_obj, dict)
    assert function_obj["name"] == "run_shell"
    assert function_obj["description"] == "Execute a shell command"
    params = function_obj["parameters"]
    assert isinstance(params, dict)
    assert params["type"] == "object"
    properties = params["properties"]
    assert isinstance(properties, dict)
    assert "command" in properties
    command_prop = properties["command"]
    assert isinstance(command_prop, dict)
    assert command_prop["type"] == "string"
    assert command_prop["description"] == "Shell command"
    assert "workdir" in properties
    assert "level" in properties
    level_prop = properties["level"]
    assert isinstance(level_prop, dict)
    assert level_prop["enum"] == ["DEBUG", "INFO", "ERROR"]
    required = params["required"]
    assert required == ["command", "level"]


def test_parse_response_with_content_no_tool_calls() -> None:
    """Test _parse_response with valid response containing content."""
    api_response: dict[str, object] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "This is the assistant's response.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }
    dmr_response = _parse_response(api_response)
    assert dmr_response.message.role == "assistant"
    assert dmr_response.message.content == "This is the assistant's response."
    assert dmr_response.message.tool_calls is None
    assert dmr_response.finish_reason == "stop"
    assert dmr_response.usage_prompt_tokens == 50
    assert dmr_response.usage_completion_tokens == 20


def test_parse_response_with_tool_calls() -> None:
    """Test _parse_response with tool_calls in response."""
    api_response: dict[str, object] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Running command",
                    "tool_calls": [
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {
                                "name": "shell",
                                "arguments": '{"cmd": "pwd"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    dmr_response = _parse_response(api_response)
    assert dmr_response.message.role == "assistant"
    assert dmr_response.message.content == "Running command"
    assert dmr_response.message.tool_calls is not None
    assert len(dmr_response.message.tool_calls) == 1
    assert dmr_response.message.tool_calls[0].tool_call_id == "call_xyz"
    assert dmr_response.message.tool_calls[0].tool_name == "shell"
    assert dmr_response.message.tool_calls[0].arguments == {"cmd": "pwd"}
    assert dmr_response.finish_reason == "tool_calls"


def test_parse_response_with_empty_choices_raises_error() -> None:
    """Test _parse_response with empty choices raises ValueError."""
    api_response: dict[str, object] = {"choices": []}
    with pytest.raises(ValueError, match="No choices in DMR response"):
        _parse_response(api_response)


def test_parse_response_with_no_message_raises_error() -> None:
    """Test _parse_response with no message in choice raises ValueError."""
    api_response: dict[str, object] = {"choices": [{"finish_reason": "stop"}]}
    with pytest.raises(ValueError, match="No message in DMR response choice"):
        _parse_response(api_response)


def test_parse_response_with_missing_usage() -> None:
    """Test _parse_response handles missing usage gracefully."""
    api_response: dict[str, object] = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Test"},
                "finish_reason": "stop",
            }
        ]
    }
    dmr_response = _parse_response(api_response)
    assert dmr_response.usage_prompt_tokens == 0
    assert dmr_response.usage_completion_tokens == 0


def test_parse_tool_calls_with_invalid_json() -> None:
    """Test _parse_tool_calls handles invalid JSON arguments gracefully."""
    raw_message: dict[str, object] = {
        "tool_calls": [
            {
                "id": "call_bad",
                "function": {
                    "name": "test_tool",
                    "arguments": "not valid json{",
                },
            }
        ]
    }
    tool_calls = _parse_tool_calls(raw_message)
    # Should return tool call with empty arguments dict
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_call_id == "call_bad"
    assert tool_calls[0].arguments == {}


def test_parse_tool_calls_with_no_tool_calls() -> None:
    """Test _parse_tool_calls returns None when no tool_calls present."""
    raw_message: dict[str, object] = {"role": "assistant", "content": "No tools used"}
    tool_calls = _parse_tool_calls(raw_message)
    assert tool_calls is None


@override_settings(
    DMR_HOST="localhost",
    DMR_PORT="8080",
    DMR_MODEL="llama-3",
    DMR_TEMPERATURE=0.1,
    DMR_MAX_TOKENS=4096,
)
@patch("agents.services.dmr_client.httpx.Client")
def test_send_chat_completion_makes_correct_http_call(
    mock_client_class: MagicMock,
) -> None:
    """Test send_chat_completion makes correct HTTP POST request."""
    # Setup mock
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Response"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client_class.return_value = mock_client

    # Call function
    config = DMRConfig(
        host="localhost", port="8080", model="llama-3", temperature=0.1, max_tokens=4096
    )
    messages = (ChatMessage(role="user", content="Hello"),)
    response = send_chat_completion(config, messages)

    # Verify HTTP call
    expected_url = "http://localhost:8080/engines/llama.cpp/v1/chat/completions"
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == expected_url
    payload = call_args[1]["json"]
    assert payload["model"] == "llama-3"
    assert payload["temperature"] == 0.1
    assert payload["max_tokens"] == 4096
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][0]["content"] == "Hello"

    # Verify response parsing
    assert response.message.role == "assistant"
    assert response.message.content == "Response"


@override_settings(
    DMR_HOST="localhost",
    DMR_PORT="8080",
    DMR_MODEL="llama-3",
    DMR_TEMPERATURE=0.1,
    DMR_MAX_TOKENS=4096,
)
@patch("agents.services.dmr_client.httpx.Client")
def test_send_chat_completion_with_tools(mock_client_class: MagicMock) -> None:
    """Test send_chat_completion includes tools when provided."""
    # Setup mock
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Using tool",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client_class.return_value = mock_client

    # Call function with tools
    config = DMRConfig(
        host="localhost", port="8080", model="llama-3", temperature=0.1, max_tokens=4096
    )
    messages = (ChatMessage(role="user", content="List files"),)
    param = ToolParameter(
        name="cmd", type="string", description="Command", required=True
    )
    tool = ToolDefinition(
        name="shell",
        description="Run shell",
        category=ToolCategory.SHELL,
        parameters=(param,),
    )
    response = send_chat_completion(config, messages, tools=(tool,))

    # Verify tools in payload
    call_args = mock_client.post.call_args
    payload = call_args[1]["json"]
    assert "tools" in payload
    assert len(payload["tools"]) == 1
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "shell"

    # Verify response
    assert response.message.tool_calls is not None
    assert len(response.message.tool_calls) == 1
    assert response.message.tool_calls[0].tool_name == "shell"


# ============================================================================
# MODEL MANAGEMENT TESTS
# ============================================================================


@patch("agents.services.dmr_model_manager.httpx.Client")
def test_list_models_returns_model_ids(mock_client_class: MagicMock) -> None:
    """Test list_models parses and normalizes the models endpoint response."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {"id": "docker.io/ai/mistral", "object": "model"},
            {"id": "docker.io/ai/qwen3-vl", "object": "model"},
        ]
    }
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client_class.return_value = mock_client

    config = DMRConfig(host="localhost", port="12434", model="ai/mistral")
    models = list_models(config)

    assert models == ["ai/mistral", "ai/qwen3-vl"]
    mock_client.get.assert_called_once_with(
        "http://localhost:12434/engines/llama.cpp/v1/models"
    )


@patch("agents.services.dmr_model_manager.httpx.Client")
def test_list_models_handles_empty_response(mock_client_class: MagicMock) -> None:
    """Test list_models returns empty list for empty data."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": []}
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client_class.return_value = mock_client

    config = DMRConfig(host="localhost", port="12434", model="test")
    models = list_models(config)

    assert models == []


@patch("agents.services.dmr_model_manager.list_models")
def test_is_model_available_true(mock_list: MagicMock) -> None:
    """Test is_model_available returns True when model exists."""
    mock_list.return_value = ["ai/mistral", "ai/qwen3-vl"]
    config = DMRConfig(host="localhost", port="12434", model="ai/mistral")

    assert is_model_available(config) is True


@patch("agents.services.dmr_model_manager.list_models")
def test_is_model_available_false(mock_list: MagicMock) -> None:
    """Test is_model_available returns False when model is missing."""
    mock_list.return_value = ["ai/qwen3-vl"]
    config = DMRConfig(host="localhost", port="12434", model="ai/mistral")

    assert is_model_available(config) is False


@patch("agents.services.dmr_model_manager.list_models")
def test_is_model_available_handles_connection_error(mock_list: MagicMock) -> None:
    """Test is_model_available returns False on connection error."""
    mock_list.side_effect = Exception("Connection refused")
    config = DMRConfig(host="localhost", port="12434", model="ai/mistral")

    assert is_model_available(config) is False


@patch("agents.services.dmr_model_manager.is_model_available")
def test_ensure_model_available_already_present(mock_check: MagicMock) -> None:
    """Test ensure_model_available returns early when model exists."""
    mock_check.return_value = True
    config = DMRConfig(host="localhost", port="12434", model="ai/mistral")

    ensure_model_available(config)

    mock_check.assert_called_once_with(config)


@patch("agents.services.dmr_model_manager.is_model_available")
def test_ensure_model_available_warns_when_missing(mock_check: MagicMock) -> None:
    """Test ensure_model_available warns when model is not found."""
    mock_check.return_value = False
    config = DMRConfig(host="192.168.1.100", port="12434", model="ai/mistral")

    ensure_model_available(config)

    mock_check.assert_called_once_with(config)


# ============================================================================
# VISION DMR CONFIG TESTS
# ============================================================================


@override_settings(
    DMR_HOST="vision-host",
    DMR_PORT="9090",
    DMR_VISION_MODEL="ai/qwen3-vl",
    DMR_TEMPERATURE=0.2,
    DMR_MAX_TOKENS=2048,
)
def test_build_vision_dmr_config_from_settings() -> None:
    """Test build_vision_dmr_config reads from Django settings."""
    config = build_vision_dmr_config()
    assert config.host == "vision-host"
    assert config.port == "9090"
    assert config.model == "ai/qwen3-vl"
    assert config.temperature == 0.2
    assert config.max_tokens == 2048


@override_settings(
    DMR_HOST="localhost",
    DMR_PORT="12434",
    DMR_VISION_MODEL="ai/qwen3-vl",
    DMR_TEMPERATURE=0.1,
    DMR_MAX_TOKENS=4096,
)
def test_build_vision_dmr_config_with_model_override() -> None:
    """Test build_vision_dmr_config with model override."""
    config = build_vision_dmr_config(model="ai/custom-vision:latest")
    assert config.host == "localhost"
    assert config.port == "12434"
    assert config.model == "ai/custom-vision:latest"
    assert config.temperature == 0.1
    assert config.max_tokens == 4096


# ============================================================================
# MODEL ID NORMALIZATION TESTS
# ============================================================================


def test_normalize_model_id_strips_docker_io_prefix() -> None:
    """Test _normalize_model_id strips docker.io/ prefix."""
    assert _normalize_model_id("docker.io/ai/mistral") == "ai/mistral"


def test_normalize_model_id_strips_docker_io_prefix_with_tag() -> None:
    """Test _normalize_model_id strips prefix from tagged models."""
    assert (
        _normalize_model_id("docker.io/ai/qwen3:30B-A3B-F16") == "ai/qwen3:30B-A3B-F16"
    )


def test_normalize_model_id_no_prefix() -> None:
    """Test _normalize_model_id passes through IDs without prefix."""
    assert _normalize_model_id("ai/mistral") == "ai/mistral"


# ============================================================================
# WARM UP MODEL TESTS
# ============================================================================


@patch("agents.services.dmr_client.send_chat_completion")
def test_warm_up_model_sends_chat_completion(mock_send: MagicMock) -> None:
    """Test warm_up_model sends a trivial chat completion to load the model."""
    config = DMRConfig(host="localhost", port="12434", model="ai/mistral")

    warm_up_model(config)

    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert call_args[0][0] is config
    messages = call_args[0][1]
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == "hi"


@patch("agents.services.dmr_client.send_chat_completion")
def test_warm_up_model_handles_failure_gracefully(mock_send: MagicMock) -> None:
    """Test warm_up_model catches exceptions and does not propagate them."""
    mock_send.side_effect = ConnectionError("DMR unavailable")
    config = DMRConfig(host="localhost", port="12434", model="ai/mistral")

    # Should not raise
    warm_up_model(config)
