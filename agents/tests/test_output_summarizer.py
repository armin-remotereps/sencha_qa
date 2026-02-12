from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import httpx
import pytest
from django.test import override_settings

from agents.services.output_summarizer import (
    _split_into_chunks,
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


def _mock_dmr_response(content: str) -> MagicMock:
    """Build a mock httpx response returning *content*."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return mock_response


def _mock_client(side_effect: object = None, return_value: object = None) -> MagicMock:
    """Build a mock httpx.Client context manager."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    if side_effect is not None:
        mock_client.post.side_effect = side_effect
    else:
        mock_client.post.return_value = return_value
    return mock_client


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
# Single-chunk AI summarization
# ============================================================================


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100, OUTPUT_SUMMARIZE_CHUNK_SIZE=6000)
@patch("agents.services.output_summarizer.httpx.Client")
def test_ai_summarization_called_for_long_output(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """Outputs exceeding threshold but fitting one chunk are summarized directly."""
    mock_client_cls.return_value = _mock_client(
        return_value=_mock_dmr_response("status: SUCCESS, reason: installed packages"),
    )

    output = "x" * 200
    result = summarize_output(
        output,
        tool_name="execute_command",
        is_error=False,
        summarizer_config=summarizer_config,
    )

    assert "[AI Summary]" in result
    assert "SUCCESS" in result
    client = mock_client_cls.return_value
    client.post.assert_called_once()


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100, OUTPUT_SUMMARIZE_CHUNK_SIZE=6000)
@patch("agents.services.output_summarizer.httpx.Client")
def test_ai_summary_includes_error_context(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """The AI prompt includes tool name and error status."""
    mock_client_cls.return_value = _mock_client(
        return_value=_mock_dmr_response("status: FAILED"),
    )

    summarize_output(
        "x" * 200,
        tool_name="execute_command",
        is_error=True,
        summarizer_config=summarizer_config,
    )

    client = mock_client_cls.return_value
    call_args = client.post.call_args
    payload = call_args[1]["json"]
    user_message = payload["messages"][1]["content"]
    assert "execute_command" in user_message
    assert "FAILED" in user_message


# ============================================================================
# Map-reduce for large outputs
# ============================================================================


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100, OUTPUT_SUMMARIZE_CHUNK_SIZE=100)
@patch("agents.services.output_summarizer.httpx.Client")
def test_map_reduce_splits_large_output(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """Output exceeding chunk size triggers map-reduce with multiple DMR calls."""
    responses = [
        _mock_dmr_response("chunk 1 summary"),
        _mock_dmr_response("chunk 2 summary"),
        _mock_dmr_response("chunk 3 summary"),
        _mock_dmr_response("final merged summary"),
    ]
    mock_client_cls.return_value = _mock_client(side_effect=responses)

    output = "A" * 100 + "B" * 100 + "C" * 100
    result = summarize_output(
        output,
        tool_name="execute_command",
        summarizer_config=summarizer_config,
    )

    assert "[AI Summary]" in result
    assert "final merged summary" in result
    client = mock_client_cls.return_value
    assert client.post.call_count == 4  # 3 map + 1 reduce


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100, OUTPUT_SUMMARIZE_CHUNK_SIZE=100)
@patch("agents.services.output_summarizer.httpx.Client")
def test_reduce_prompt_contains_all_chunk_summaries(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """The reduce step receives all chunk summaries in its prompt."""
    responses = [
        _mock_dmr_response("summary-alpha"),
        _mock_dmr_response("summary-beta"),
        _mock_dmr_response("merged result"),
    ]
    mock_client_cls.return_value = _mock_client(side_effect=responses)

    output = "X" * 200
    summarize_output(
        output,
        tool_name="execute_command",
        summarizer_config=summarizer_config,
    )

    client = mock_client_cls.return_value
    reduce_call = client.post.call_args_list[-1]
    reduce_payload = reduce_call[1]["json"]
    reduce_prompt = reduce_payload["messages"][1]["content"]
    assert "summary-alpha" in reduce_prompt
    assert "summary-beta" in reduce_prompt
    assert "Chunk 1" in reduce_prompt
    assert "Chunk 2" in reduce_prompt


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100, OUTPUT_SUMMARIZE_CHUNK_SIZE=150)
@patch("agents.services.output_summarizer.httpx.Client")
def test_two_chunks_exact_boundary(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """Output exactly twice the chunk size produces 2 map calls + 1 reduce."""
    responses = [
        _mock_dmr_response("first half"),
        _mock_dmr_response("second half"),
        _mock_dmr_response("combined"),
    ]
    mock_client_cls.return_value = _mock_client(side_effect=responses)

    output = "Y" * 300
    result = summarize_output(
        output,
        tool_name="execute_command",
        summarizer_config=summarizer_config,
    )

    assert "combined" in result
    client = mock_client_cls.return_value
    assert client.post.call_count == 3


# ============================================================================
# Fallback on DMR error
# ============================================================================


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100, OUTPUT_SUMMARIZE_CHUNK_SIZE=6000)
@patch("agents.services.output_summarizer.httpx.Client")
def test_fallback_to_truncation_on_dmr_error(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    """When the DMR call fails, output is truncated instead."""
    mock_client_cls.return_value = _mock_client(
        side_effect=httpx.ConnectError("connection refused"),
    )

    output = "A" * 50 + "B" * 150
    result = summarize_output(
        output,
        tool_name="execute_command",
        summarizer_config=summarizer_config,
    )

    assert "[output truncated]" in result
    assert "[AI Summary]" not in result


@override_settings(OUTPUT_SUMMARIZE_THRESHOLD=100, OUTPUT_SUMMARIZE_CHUNK_SIZE=6000)
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
# Chunk splitting helper
# ============================================================================


def test_split_into_chunks_single() -> None:
    """Text shorter than chunk size returns a single chunk."""
    result = _split_into_chunks("hello", 100)
    assert result == ["hello"]


def test_split_into_chunks_exact() -> None:
    """Text exactly at chunk size returns a single chunk."""
    result = _split_into_chunks("A" * 100, 100)
    assert result == ["A" * 100]


def test_split_into_chunks_multiple() -> None:
    """Text larger than chunk size is split into multiple chunks."""
    result = _split_into_chunks("A" * 250, 100)
    assert len(result) == 3
    assert result[0] == "A" * 100
    assert result[1] == "A" * 100
    assert result[2] == "A" * 50


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
