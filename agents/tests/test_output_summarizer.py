from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.test import override_settings

from agents.services.output_summarizer import (
    _truncate_output,
    summarize_output,
)
from agents.types import DMRConfig


@pytest.fixture
def summarizer_config() -> DMRConfig:
    return DMRConfig(
        host="localhost",
        port="12434",
        model="ai/mistral",
        temperature=0.0,
        max_tokens=512,
    )


# ============================================================================
# Pass-through for short outputs
# ============================================================================


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=2000)
def test_short_output_returned_unchanged(summarizer_config: DMRConfig) -> None:
    """Output under the threshold is returned as-is."""
    output = "hello world"
    result = summarize_output(
        output,
        tool_name="execute_command",
        summarizer_config=summarizer_config,
    )
    assert result == "hello world"


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=2000)
def test_exactly_threshold_returned_unchanged(summarizer_config: DMRConfig) -> None:
    """Output exactly at the threshold is returned as-is."""
    output = "x" * 2000
    result = summarize_output(
        output,
        tool_name="execute_command",
        summarizer_config=summarizer_config,
    )
    assert result == output


# ============================================================================
# AI summarization for long outputs
# ============================================================================


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100)
@patch("agents.services.output_summarizer.httpx.Client")
def test_ai_summarization_called_for_long_output(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """Outputs exceeding threshold are sent to the AI summarizer."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "status: SUCCESS, reason: installed packages",
                }
            }
        ]
    }
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    output = "x" * 200
    result = summarize_output(
        output,
        tool_name="execute_command",
        is_error=False,
        summarizer_config=summarizer_config,
    )

    assert "[AI Summary]" in result
    assert "SUCCESS" in result
    mock_client.post.assert_called_once()


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100)
@patch("agents.services.output_summarizer.httpx.Client")
def test_ai_summary_includes_error_context(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """The AI prompt includes tool name and error status."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "status: FAILED"}}]
    }
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    summarize_output(
        "x" * 200,
        tool_name="execute_command",
        is_error=True,
        summarizer_config=summarizer_config,
    )

    call_args = mock_client.post.call_args
    payload = call_args[1]["json"]
    user_message = payload["messages"][1]["content"]
    assert "execute_command" in user_message
    assert "FAILED" in user_message


# ============================================================================
# Fallback on DMR error
# ============================================================================


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100)
@patch("agents.services.output_summarizer.httpx.Client")
def test_fallback_to_truncation_on_dmr_error(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """When the DMR call fails, output is truncated instead."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    mock_client_cls.return_value = mock_client

    output = "A" * 50 + "B" * 150
    result = summarize_output(
        output,
        tool_name="execute_command",
        summarizer_config=summarizer_config,
    )

    assert "[output truncated]" in result
    assert "[AI Summary]" not in result


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100)
def test_fallback_when_no_summarizer_config() -> None:
    """Without a summarizer config, long output is truncated."""
    output = "x" * 200
    result = summarize_output(
        output,
        tool_name="execute_command",
        summarizer_config=None,
    )

    assert "[output truncated]" in result


# ============================================================================
# Truncation helper
# ============================================================================


def test_truncate_output_75_25_split() -> None:
    """_truncate_output uses a 75/25 head/tail split."""
    output = "H" * 750 + "T" * 250
    result = _truncate_output(output, max_length=100)

    # Head portion = 75 chars, tail portion = 25 chars
    assert result.startswith("H" * 75)
    assert result.endswith("T" * 25)
    assert "[output truncated]" in result


def test_truncate_output_preserves_content() -> None:
    """Truncated output preserves head and tail sections."""
    head = "HEAD-CONTENT-" * 20
    tail = "-TAIL-CONTENT" * 20
    output = head + tail
    result = _truncate_output(output, max_length=200)

    assert result.startswith("HEAD-CONTENT-")
    assert result.endswith("ONTENT")
    assert "[output truncated]" in result
