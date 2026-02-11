from __future__ import annotations

from unittest.mock import MagicMock, patch

from agents.services.vision_qa import answer_screenshot_question
from agents.types import (
    ChatMessage,
    DMRConfig,
    DMRResponse,
    ImageContent,
    TextContent,
)


def _make_config() -> DMRConfig:
    return DMRConfig(
        host="localhost",
        port="12434",
        model="ai/qwen3-vl",
        temperature=0.1,
        max_tokens=4096,
    )


# ============================================================================
# answer_screenshot_question tests
# ============================================================================


@patch("agents.services.vision_qa.send_chat_completion")
def test_answer_screenshot_question_success(mock_send: MagicMock) -> None:
    """Returns text answer from vision model."""
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="The button is blue."),
        finish_reason="stop",
        usage_prompt_tokens=50,
        usage_completion_tokens=10,
    )

    result = answer_screenshot_question(
        vision_config=_make_config(),
        image_base64="iVBORw0KGgoAAAANS",
        question="What color is the button?",
    )

    assert result == "The button is blue."


@patch("agents.services.vision_qa.send_chat_completion")
def test_answer_screenshot_question_sends_correct_messages(
    mock_send: MagicMock,
) -> None:
    """Verifies system prompt + image content sent correctly."""
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="Yes"),
        finish_reason="stop",
        usage_prompt_tokens=50,
        usage_completion_tokens=5,
    )

    answer_screenshot_question(
        vision_config=_make_config(),
        image_base64="abc123base64data",
        question="Is there a login form?",
    )

    mock_send.assert_called_once()
    call_args = mock_send.call_args
    messages = call_args[0][1]

    # First message is system prompt
    assert messages[0].role == "system"
    assert isinstance(messages[0].content, str)
    assert "Answer the question" in messages[0].content

    # Second message is user with multimodal content
    assert messages[1].role == "user"
    assert isinstance(messages[1].content, tuple)
    assert len(messages[1].content) == 2

    text_part = messages[1].content[0]
    assert isinstance(text_part, TextContent)
    assert text_part.text == "Is there a login form?"

    image_part = messages[1].content[1]
    assert isinstance(image_part, ImageContent)
    assert image_part.base64_data == "abc123base64data"


@patch("agents.services.vision_qa.send_chat_completion")
def test_answer_screenshot_question_non_string_response(
    mock_send: MagicMock,
) -> None:
    """Returns fallback when response content is None."""
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content=None),
        finish_reason="stop",
        usage_prompt_tokens=50,
        usage_completion_tokens=0,
    )

    result = answer_screenshot_question(
        vision_config=_make_config(),
        image_base64="iVBORw0KGgoAAAANS",
        question="What is shown?",
    )

    assert result == "Unable to answer the question."


@patch("agents.services.vision_qa.send_chat_completion")
def test_answer_screenshot_question_passes_config(mock_send: MagicMock) -> None:
    """Verifies DMRConfig is passed through to send_chat_completion."""
    config = DMRConfig(
        host="remote-host",
        port="9999",
        model="ai/custom-vision",
        temperature=0.5,
        max_tokens=2048,
    )
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="answer"),
        finish_reason="stop",
        usage_prompt_tokens=10,
        usage_completion_tokens=5,
    )

    answer_screenshot_question(
        vision_config=config,
        image_base64="data",
        question="question",
    )

    mock_send.assert_called_once()
    call_args = mock_send.call_args
    passed_config = call_args[0][0]
    assert passed_config is config
    assert passed_config.host == "remote-host"
    assert passed_config.port == "9999"
    assert passed_config.model == "ai/custom-vision"
