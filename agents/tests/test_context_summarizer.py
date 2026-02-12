from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.test import override_settings

from agents.services.context_summarizer import (
    _estimate_context_size,
    _estimate_message_size,
    _serialize_messages_for_summary,
    _split_messages,
    summarize_context_if_needed,
)
from agents.types import (
    ChatMessage,
    DMRConfig,
    ImageContent,
    TextContent,
    ToolCall,
)


@pytest.fixture
def summarizer_config() -> DMRConfig:
    return DMRConfig(
        host="localhost",
        port="12434",
        model="ai/mistral",
        temperature=0.0,
        max_tokens=512,
    )


def _make_messages(
    *,
    system: str = "You are a test agent.",
    user: str = "Do the task.",
    middle_count: int = 0,
    middle_content: str = "x" * 100,
    tail_count: int = 2,
    tail_content: str = "recent",
) -> list[ChatMessage]:
    msgs: list[ChatMessage] = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]
    for i in range(middle_count):
        msgs.append(
            ChatMessage(
                role="assistant",
                content=f"{middle_content}-{i}",
                tool_calls=(
                    ToolCall(
                        tool_call_id=f"tc-mid-{i}",
                        tool_name="execute_command",
                        arguments={"command": "echo hi"},
                    ),
                ),
            )
        )
        msgs.append(
            ChatMessage(
                role="tool",
                content=f"result-{i}",
                tool_call_id=f"tc-mid-{i}",
            )
        )
    for i in range(tail_count):
        msgs.append(ChatMessage(role="assistant", content=f"{tail_content}-{i}"))
    return msgs


def _mock_dmr_response(content: str) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return mock_response


def _mock_client(
    side_effect: Exception | list[MagicMock] | None = None,
    return_value: MagicMock | None = None,
) -> MagicMock:
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    if side_effect is not None:
        mock.post.side_effect = side_effect
    else:
        mock.post.return_value = return_value
    return mock


# ============================================================================
# Pass-through for short context
# ============================================================================


@override_settings(CONTEXT_SUMMARIZE_THRESHOLD=50000, CONTEXT_PRESERVE_LAST_MESSAGES=6)
def test_short_context_returned_unchanged(summarizer_config: DMRConfig) -> None:
    messages = _make_messages(middle_count=2)
    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)
    assert result == messages


@override_settings(CONTEXT_SUMMARIZE_THRESHOLD=50000, CONTEXT_PRESERVE_LAST_MESSAGES=6)
def test_at_threshold_returned_unchanged(summarizer_config: DMRConfig) -> None:
    messages = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="u"),
        ChatMessage(role="assistant", content="a"),
    ]
    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)
    assert result == messages


# ============================================================================
# Empty middle pass-through
# ============================================================================


@override_settings(CONTEXT_SUMMARIZE_THRESHOLD=10, CONTEXT_PRESERVE_LAST_MESSAGES=6)
def test_empty_middle_passthrough(summarizer_config: DMRConfig) -> None:
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="task"),
        ChatMessage(role="assistant", content="done"),
    ]
    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)
    assert result == messages


# ============================================================================
# Summarization trigger
# ============================================================================


@override_settings(
    CONTEXT_SUMMARIZE_THRESHOLD=100,
    CONTEXT_PRESERVE_LAST_MESSAGES=2,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=8000,
    DMR_REQUEST_TIMEOUT=10,
)
@patch("agents.services.context_summarizer.httpx.Client")
def test_summarization_triggered_above_threshold(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    mock_client_cls.return_value = _mock_client(
        return_value=_mock_dmr_response("Summary of actions taken"),
    )

    messages = _make_messages(middle_count=5, middle_content="x" * 200, tail_count=1)
    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)

    # Should have prefix (2) + summary (1) + tail
    assert any(
        isinstance(m.content, str) and "[Context Summary]" in m.content for m in result
    )
    assert len(result) < len(messages)

    client = mock_client_cls.return_value
    client.post.assert_called()


# ============================================================================
# System + user preserved at positions 0 and 1
# ============================================================================


@override_settings(
    CONTEXT_SUMMARIZE_THRESHOLD=100,
    CONTEXT_PRESERVE_LAST_MESSAGES=2,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=8000,
    DMR_REQUEST_TIMEOUT=10,
)
@patch("agents.services.context_summarizer.httpx.Client")
def test_system_and_user_preserved(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    mock_client_cls.return_value = _mock_client(
        return_value=_mock_dmr_response("summary"),
    )

    messages = _make_messages(middle_count=5, middle_content="x" * 200, tail_count=1)
    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)

    assert result[0].role == "system"
    assert result[0].content == "You are a test agent."
    assert result[1].role == "user"


# ============================================================================
# Last N messages preserved
# ============================================================================


@override_settings(
    CONTEXT_SUMMARIZE_THRESHOLD=100,
    CONTEXT_PRESERVE_LAST_MESSAGES=4,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=8000,
    DMR_REQUEST_TIMEOUT=10,
)
@patch("agents.services.context_summarizer.httpx.Client")
def test_tail_messages_preserved(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    mock_client_cls.return_value = _mock_client(
        return_value=_mock_dmr_response("summary"),
    )

    messages = _make_messages(middle_count=5, middle_content="x" * 200, tail_count=2)
    original_tail = messages[-4:]
    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)

    # Last 4 messages should be preserved
    assert result[-4:] == original_tail


# ============================================================================
# Orphan tool result prevention
# ============================================================================


def test_split_messages_no_orphan_tool_results() -> None:
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="task"),
        ChatMessage(role="assistant", content="thinking-1"),
        ChatMessage(
            role="assistant",
            content="calling tool",
            tool_calls=(
                ToolCall(
                    tool_call_id="tc1",
                    tool_name="execute_command",
                    arguments={"command": "ls"},
                ),
            ),
        ),
        ChatMessage(role="tool", content="file1.txt", tool_call_id="tc1"),
        ChatMessage(role="assistant", content="recent-1"),
        ChatMessage(role="assistant", content="recent-2"),
    ]

    # preserve_count=3 would normally start tail at index 4 (the tool result)
    _, middle, tail = _split_messages(messages, 3)

    # The tool result should NOT be orphaned from its assistant message
    # Both the assistant (with tool_calls) and tool result should be in the same group
    for msg in tail:
        if msg.role == "tool":
            # Verify the preceding assistant message is also in tail
            idx = tail.index(msg)
            assert idx > 0
            assert tail[idx - 1].role == "assistant"


# ============================================================================
# Re-summarization (existing summary included, not stacked)
# ============================================================================


@override_settings(
    CONTEXT_SUMMARIZE_THRESHOLD=50,
    CONTEXT_PRESERVE_LAST_MESSAGES=2,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=8000,
    DMR_REQUEST_TIMEOUT=10,
)
@patch("agents.services.context_summarizer.httpx.Client")
def test_resummarization_includes_old_summary(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    mock_client_cls.return_value = _mock_client(
        return_value=_mock_dmr_response("new comprehensive summary"),
    )

    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="task"),
        ChatMessage(
            role="user",
            content="[Context Summary] old summary of previous actions",
        ),
        ChatMessage(role="assistant", content="x" * 100),
        ChatMessage(role="assistant", content="x" * 100),
        ChatMessage(role="assistant", content="recent"),
    ]

    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)

    # Should have exactly one summary message (not stacked)
    summary_count = sum(
        1
        for m in result
        if isinstance(m.content, str) and "[Context Summary]" in m.content
    )
    assert summary_count == 1

    # The old summary should have been included in the middle for re-summarization
    client = mock_client_cls.return_value
    call_args = client.post.call_args
    payload = call_args[1]["json"]
    user_prompt = payload["messages"][1]["content"]
    assert "old summary" in user_prompt


# ============================================================================
# Multimodal content size estimation
# ============================================================================


def test_estimate_message_size_with_image_content() -> None:
    base64_data = "A" * 5000
    message = ChatMessage(
        role="user",
        content=(
            TextContent(text="Look at this image"),
            ImageContent(base64_data=base64_data),
        ),
    )
    size = _estimate_message_size(message)
    assert size >= 5000 + len("Look at this image")


def test_estimate_message_size_none_content() -> None:
    message = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=(
            ToolCall(
                tool_call_id="tc1",
                tool_name="execute_command",
                arguments={"command": "ls"},
            ),
        ),
    )
    size = _estimate_message_size(message)
    # Should count tool_call name + args but not content
    assert size > 0


def test_estimate_message_size_none_content_no_tools() -> None:
    message = ChatMessage(role="assistant", content=None)
    size = _estimate_message_size(message)
    assert size == 0


# ============================================================================
# DMR failure fallback to truncation
# ============================================================================


@override_settings(
    CONTEXT_SUMMARIZE_THRESHOLD=100,
    CONTEXT_PRESERVE_LAST_MESSAGES=2,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=8000,
    DMR_REQUEST_TIMEOUT=10,
)
@patch("agents.services.context_summarizer.httpx.Client")
def test_fallback_to_truncation_on_dmr_error(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    mock_client_cls.return_value = _mock_client(
        side_effect=httpx.ConnectError("connection refused"),
    )

    messages = _make_messages(middle_count=5, middle_content="x" * 200, tail_count=1)
    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)

    # Should still produce a summary message (with truncated content)
    assert any(
        isinstance(m.content, str) and "[Context Summary]" in m.content for m in result
    )
    assert any(
        isinstance(m.content, str) and "[context truncated]" in m.content
        for m in result
    )


# ============================================================================
# No summarizer config fallback
# ============================================================================


@override_settings(
    CONTEXT_SUMMARIZE_THRESHOLD=100,
    CONTEXT_PRESERVE_LAST_MESSAGES=2,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=8000,
)
def test_fallback_when_no_summarizer_config() -> None:
    messages = _make_messages(middle_count=5, middle_content="x" * 200, tail_count=1)
    result = summarize_context_if_needed(messages, summarizer_config=None)

    assert any(
        isinstance(m.content, str) and "[context truncated]" in m.content
        for m in result
    )


# ============================================================================
# Map-reduce for large middle sections
# ============================================================================


@override_settings(
    CONTEXT_SUMMARIZE_THRESHOLD=100,
    CONTEXT_PRESERVE_LAST_MESSAGES=2,
    CONTEXT_SUMMARIZE_CHUNK_SIZE=200,
    DMR_REQUEST_TIMEOUT=10,
)
@patch("agents.services.context_summarizer.httpx.Client")
def test_map_reduce_for_large_middle(
    mock_client_cls: MagicMock,
    summarizer_config: DMRConfig,
) -> None:
    responses = [
        _mock_dmr_response("chunk 1 summary"),
        _mock_dmr_response("chunk 2 summary"),
        _mock_dmr_response("chunk 3 summary"),
        _mock_dmr_response("final merged summary"),
    ]
    mock_client_cls.return_value = _mock_client(side_effect=responses)

    messages = _make_messages(middle_count=10, middle_content="x" * 200, tail_count=1)
    result = summarize_context_if_needed(messages, summarizer_config=summarizer_config)

    assert any(
        isinstance(m.content, str) and "[Context Summary]" in m.content for m in result
    )
    client = mock_client_cls.return_value
    assert client.post.call_count >= 2  # at least map + reduce


# ============================================================================
# Helper function tests
# ============================================================================


def test_estimate_context_size_basic() -> None:
    messages = [
        ChatMessage(role="system", content="hello"),
        ChatMessage(role="user", content="world"),
    ]
    size = _estimate_context_size(messages)
    assert size == len("hello") + len("world")


def test_estimate_context_size_with_tool_calls() -> None:
    messages = [
        ChatMessage(
            role="assistant",
            content="thinking",
            tool_calls=(
                ToolCall(
                    tool_call_id="tc1",
                    tool_name="execute_command",
                    arguments={"command": "ls -la"},
                ),
            ),
        ),
    ]
    size = _estimate_context_size(messages)
    assert size > len("thinking")


def test_serialize_messages_for_summary_basic() -> None:
    messages = [
        ChatMessage(role="assistant", content="I will run a command"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=(
                ToolCall(
                    tool_call_id="tc1",
                    tool_name="execute_command",
                    arguments={"command": "ls"},
                ),
            ),
        ),
        ChatMessage(role="tool", content="file1.txt\nfile2.txt", tool_call_id="tc1"),
    ]
    text = _serialize_messages_for_summary(messages)

    assert "[ASSISTANT] I will run a command" in text
    assert "[TOOL_CALL] execute_command" in text
    assert "[TOOL_RESULT] file1.txt" in text


def test_serialize_messages_replaces_image_with_placeholder() -> None:
    messages = [
        ChatMessage(
            role="user",
            content=(
                TextContent(text="Look at this"),
                ImageContent(base64_data="AAAA" * 1000),
            ),
        ),
    ]
    text = _serialize_messages_for_summary(messages)

    assert "[IMAGE]" in text
    assert "AAAA" not in text


def test_split_messages_basic() -> None:
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="task"),
        ChatMessage(role="assistant", content="m1"),
        ChatMessage(role="assistant", content="m2"),
        ChatMessage(role="assistant", content="m3"),
        ChatMessage(role="assistant", content="tail1"),
        ChatMessage(role="assistant", content="tail2"),
    ]
    prefix, middle, tail = _split_messages(messages, 2)

    assert len(prefix) == 2
    assert prefix[0].content == "sys"
    assert prefix[1].content == "task"
    assert len(tail) == 2
    assert tail[0].content == "tail1"
    assert tail[1].content == "tail2"
    assert len(middle) == 3
