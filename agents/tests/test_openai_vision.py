from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import override_settings

from agents.services.dmr_client import (
    _build_headers,
    _build_url,
    _get_timeout,
    send_chat_completion,
)
from agents.services.dmr_config import (
    build_openai_vision_config,
    build_vision_config,
)
from agents.types import ChatMessage, DMRConfig

# ============================================================================
# CONFIG BUILDER TESTS
# ============================================================================


@override_settings(
    OPENAI_API_KEY="sk-test-key",
    OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions",
    OPENAI_VISION_MODEL="gpt-4o",
    OPENAI_TEMPERATURE=0.1,
    OPENAI_MAX_TOKENS=4096,
)
def test_build_openai_vision_config() -> None:
    """Test build_openai_vision_config reads from Django settings."""
    config = build_openai_vision_config()
    assert config.api_key == "sk-test-key"
    assert config.base_url == "https://api.openai.com/v1/chat/completions"
    assert config.model == "gpt-4o"
    assert config.temperature == 0.1
    assert config.max_tokens == 4096


@override_settings(
    OPENAI_API_KEY="sk-test",
    OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions",
    OPENAI_VISION_MODEL="gpt-4o",
    OPENAI_TEMPERATURE=0.1,
    OPENAI_MAX_TOKENS=4096,
)
def test_build_openai_vision_config_with_model_override() -> None:
    """Test build_openai_vision_config allows model override."""
    config = build_openai_vision_config(model="gpt-5")
    assert config.model == "gpt-5"
    assert config.api_key == "sk-test"


@override_settings(
    VISION_BACKEND="dmr",
    DMR_HOST="localhost",
    DMR_PORT="12434",
    DMR_VISION_MODEL="ai/qwen3-vl",
    DMR_TEMPERATURE=0.1,
    DMR_MAX_TOKENS=4096,
)
def test_build_vision_config_dmr_default() -> None:
    """Test build_vision_config returns DMR config when VISION_BACKEND=dmr."""
    config = build_vision_config()
    assert config.api_key is None
    assert config.base_url is None
    assert config.model == "ai/qwen3-vl"
    assert config.host == "localhost"
    assert config.port == "12434"


@override_settings(
    VISION_BACKEND="openai",
    OPENAI_API_KEY="sk-openai-key",
    OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions",
    OPENAI_VISION_MODEL="gpt-4o",
    OPENAI_TEMPERATURE=0.2,
    OPENAI_MAX_TOKENS=8192,
)
def test_build_vision_config_openai() -> None:
    """Test build_vision_config returns OpenAI config when VISION_BACKEND=openai."""
    config = build_vision_config()
    assert config.api_key == "sk-openai-key"
    assert config.base_url == "https://api.openai.com/v1/chat/completions"
    assert config.model == "gpt-4o"
    assert config.temperature == 0.2
    assert config.max_tokens == 8192


# ============================================================================
# URL / HEADER / TIMEOUT HELPER TESTS
# ============================================================================


def test_build_url_with_base_url() -> None:
    """Test _build_url returns base_url when set."""
    config = DMRConfig(
        host="localhost",
        port="8080",
        model="gpt-4o",
        base_url="https://api.openai.com/v1/chat/completions",
    )
    assert _build_url(config) == "https://api.openai.com/v1/chat/completions"


def test_build_url_without_base_url() -> None:
    """Test _build_url constructs DMR URL when base_url is None."""
    config = DMRConfig(host="localhost", port="8080", model="ai/mistral")
    assert (
        _build_url(config)
        == "http://localhost:8080/engines/llama.cpp/v1/chat/completions"
    )


def test_build_headers_with_api_key() -> None:
    """Test _build_headers returns Bearer auth when api_key is set."""
    config = DMRConfig(host="", port="", model="gpt-4o", api_key="sk-test-key")
    headers = _build_headers(config)
    assert headers == {"Authorization": "Bearer sk-test-key"}


def test_build_headers_without_api_key() -> None:
    """Test _build_headers returns empty dict when api_key is None."""
    config = DMRConfig(host="localhost", port="8080", model="ai/mistral")
    headers = _build_headers(config)
    assert headers == {}


@override_settings(OPENAI_REQUEST_TIMEOUT=120)
def test_get_timeout_with_api_key() -> None:
    """Test _get_timeout returns OPENAI_REQUEST_TIMEOUT when api_key is set."""
    config = DMRConfig(host="", port="", model="gpt-4o", api_key="sk-test")
    assert _get_timeout(config) == 120.0


@override_settings(DMR_REQUEST_TIMEOUT=600)
def test_get_timeout_without_api_key() -> None:
    """Test _get_timeout returns DMR_REQUEST_TIMEOUT when api_key is None."""
    config = DMRConfig(host="localhost", port="8080", model="ai/mistral")
    assert _get_timeout(config) == 600.0


# ============================================================================
# SEND CHAT COMPLETION AUTH TESTS
# ============================================================================


@override_settings(OPENAI_REQUEST_TIMEOUT=120)
@patch("agents.services.dmr_client.httpx.Client")
def test_send_chat_completion_openai_auth_header(
    mock_client_class: MagicMock,
) -> None:
    """Test send_chat_completion sends Bearer header for OpenAI config."""
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

    config = DMRConfig(
        host="",
        port="",
        model="gpt-4o",
        temperature=0.1,
        max_tokens=4096,
        api_key="sk-test-key-123",
        base_url="https://api.openai.com/v1/chat/completions",
    )
    messages = (ChatMessage(role="user", content="Hello"),)
    send_chat_completion(config, messages)

    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://api.openai.com/v1/chat/completions"
    assert call_args[1]["headers"] == {"Authorization": "Bearer sk-test-key-123"}

    payload = call_args[1]["json"]
    assert "keep_alive" not in payload


@override_settings(DMR_REQUEST_TIMEOUT=600)
@patch("agents.services.dmr_client.httpx.Client")
def test_send_chat_completion_dmr_no_auth(
    mock_client_class: MagicMock,
) -> None:
    """Test send_chat_completion sends no auth header for DMR config."""
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

    config = DMRConfig(
        host="localhost",
        port="8080",
        model="ai/mistral",
        temperature=0.1,
        max_tokens=4096,
    )
    messages = (ChatMessage(role="user", content="Hello"),)
    send_chat_completion(config, messages)

    call_args = mock_client.post.call_args
    assert (
        call_args[0][0] == "http://localhost:8080/engines/llama.cpp/v1/chat/completions"
    )
    assert call_args[1]["headers"] == {}


@override_settings(DMR_REQUEST_TIMEOUT=600)
@patch("agents.services.dmr_client.httpx.Client")
def test_send_chat_completion_dmr_keeps_keep_alive(
    mock_client_class: MagicMock,
) -> None:
    """Test send_chat_completion includes keep_alive for DMR config."""
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

    config = DMRConfig(
        host="localhost",
        port="8080",
        model="ai/mistral",
        temperature=0.1,
        max_tokens=4096,
    )
    messages = (ChatMessage(role="user", content="Hello"),)
    send_chat_completion(config, messages, keep_alive=300)

    payload = mock_client.post.call_args[1]["json"]
    assert payload["keep_alive"] == 300


@override_settings(OPENAI_REQUEST_TIMEOUT=120)
@patch("agents.services.dmr_client.httpx.Client")
def test_send_chat_completion_openai_skips_keep_alive(
    mock_client_class: MagicMock,
) -> None:
    """Test send_chat_completion skips keep_alive for OpenAI config."""
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

    config = DMRConfig(
        host="",
        port="",
        model="gpt-4o",
        temperature=0.1,
        max_tokens=4096,
        api_key="sk-test",
        base_url="https://api.openai.com/v1/chat/completions",
    )
    messages = (ChatMessage(role="user", content="Hello"),)
    send_chat_completion(config, messages, keep_alive=300)

    payload = mock_client.post.call_args[1]["json"]
    assert "keep_alive" not in payload
