from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.exceptions import ElementNotFoundError
from agents.services.controller_element_finder import (
    _parse_coordinates,
    _query_vision_model,
    find_element_coordinates,
)
from agents.types import ChatMessage, DMRConfig, DMRResponse


@pytest.fixture
def mock_vision_config() -> DMRConfig:
    return DMRConfig(
        host="test", port="8080", model="test-vision", temperature=0.0, max_tokens=1000
    )


def test_find_element_coordinates_uses_omniparser_when_configured(
    mock_vision_config: DMRConfig,
) -> None:
    with patch(
        "agents.services.controller_element_finder.is_omniparser_configured"
    ) as mock_is_omni, patch(
        "agents.services.controller_element_finder.find_element_coordinates_omniparser"
    ) as mock_omni_finder:
        mock_is_omni.return_value = True
        mock_omni_finder.return_value = (123, 456)

        result = find_element_coordinates(
            1, "test button", mock_vision_config, on_screenshot=None
        )

        assert result == (123, 456)
        mock_omni_finder.assert_called_once_with(
            1, "test button", mock_vision_config, on_screenshot=None
        )


def test_find_element_coordinates_uses_vision_when_omniparser_not_configured(
    mock_vision_config: DMRConfig,
) -> None:
    with patch(
        "agents.services.controller_element_finder.is_omniparser_configured"
    ) as mock_is_omni, patch(
        "agents.services.controller_element_finder.controller_screenshot"
    ) as mock_screenshot, patch(
        "agents.services.controller_element_finder._query_vision_model"
    ) as mock_query:
        mock_is_omni.return_value = False
        mock_screenshot.return_value = {"image_base64": "base64data"}
        mock_query.return_value = (250, 180)

        result = find_element_coordinates(
            1, "OK button", mock_vision_config, on_screenshot=None
        )

        assert result == (250, 180)
        mock_screenshot.assert_called_once_with(1)
        mock_query.assert_called_once_with(
            "base64data", "OK button", mock_vision_config
        )


def test_find_element_coordinates_calls_screenshot_callback(
    mock_vision_config: DMRConfig,
) -> None:
    mock_callback = MagicMock()

    with patch(
        "agents.services.controller_element_finder.is_omniparser_configured"
    ) as mock_is_omni, patch(
        "agents.services.controller_element_finder.controller_screenshot"
    ) as mock_screenshot, patch(
        "agents.services.controller_element_finder._query_vision_model"
    ) as mock_query:
        mock_is_omni.return_value = False
        mock_screenshot.return_value = {"image_base64": "base64image"}
        mock_query.return_value = (100, 200)

        result = find_element_coordinates(
            1, "button", mock_vision_config, on_screenshot=mock_callback
        )

        assert result == (100, 200)
        mock_callback.assert_called_once_with(
            "base64image", "controller_element_finder"
        )


def test_query_vision_model_success(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.controller_element_finder.send_chat_completion"
    ) as mock_send:
        mock_send.return_value = DMRResponse(
            message=ChatMessage(role="assistant", content="450,320"),
            finish_reason="stop",
            usage_prompt_tokens=100,
            usage_completion_tokens=10,
        )

        result = _query_vision_model("base64data", "test element", mock_vision_config)

        assert result == (450, 320)
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == mock_vision_config
        messages = call_args[0][1]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert "UI element locator" in messages[0].content
        assert messages[1].role == "user"


def test_query_vision_model_not_found(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.controller_element_finder.send_chat_completion"
    ) as mock_send:
        mock_send.return_value = DMRResponse(
            message=ChatMessage(role="assistant", content="NOT_FOUND"),
            finish_reason="stop",
            usage_prompt_tokens=100,
            usage_completion_tokens=5,
        )

        with pytest.raises(ElementNotFoundError) as exc_info:
            _query_vision_model("base64data", "missing element", mock_vision_config)

        assert "Element not found on screen: missing element" in str(exc_info.value)


def test_query_vision_model_ambiguous(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.controller_element_finder.send_chat_completion"
    ) as mock_send:
        mock_send.return_value = DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="AMBIGUOUS: Multiple buttons found with that description",
            ),
            finish_reason="stop",
            usage_prompt_tokens=100,
            usage_completion_tokens=10,
        )

        with pytest.raises(ElementNotFoundError) as exc_info:
            _query_vision_model("base64data", "button", mock_vision_config)

        assert "Ambiguous element on screen: button" in str(exc_info.value)
        assert "Multiple buttons found" in str(exc_info.value)


def test_query_vision_model_no_response(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.controller_element_finder.send_chat_completion"
    ) as mock_send:
        mock_send.return_value = DMRResponse(
            message=ChatMessage(role="assistant", content=None),
            finish_reason="stop",
            usage_prompt_tokens=100,
            usage_completion_tokens=0,
        )

        with pytest.raises(ElementNotFoundError) as exc_info:
            _query_vision_model("base64data", "element", mock_vision_config)

        assert "Vision model returned no response" in str(exc_info.value)


def test_parse_coordinates_success() -> None:
    result = _parse_coordinates("450,320", "test element")
    assert result == (450, 320)


def test_parse_coordinates_with_spaces() -> None:
    result = _parse_coordinates("450, 320", "test element")
    assert result == (450, 320)


def test_parse_coordinates_with_extra_text() -> None:
    result = _parse_coordinates(
        "The element is at 450,320 on the screen", "test element"
    )
    assert result == (450, 320)


def test_parse_coordinates_not_found() -> None:
    with pytest.raises(ElementNotFoundError) as exc_info:
        _parse_coordinates("NOT_FOUND", "missing element")
    assert "Element not found on screen: missing element" in str(exc_info.value)


def test_parse_coordinates_ambiguous() -> None:
    with pytest.raises(ElementNotFoundError) as exc_info:
        _parse_coordinates("AMBIGUOUS: Multiple matches", "button")
    assert "Ambiguous element on screen: button" in str(exc_info.value)


def test_parse_coordinates_invalid_format() -> None:
    with pytest.raises(ElementNotFoundError) as exc_info:
        _parse_coordinates("no coordinates here", "element")
    assert "Could not parse coordinates from vision response" in str(exc_info.value)


def test_parse_coordinates_single_number() -> None:
    with pytest.raises(ElementNotFoundError) as exc_info:
        _parse_coordinates("450", "element")
    assert "Could not parse coordinates from vision response" in str(exc_info.value)


def test_parse_coordinates_large_numbers() -> None:
    result = _parse_coordinates("1920,1080", "bottom right corner")
    assert result == (1920, 1080)


def test_parse_coordinates_zero_values() -> None:
    result = _parse_coordinates("0,0", "top left corner")
    assert result == (0, 0)


def test_query_vision_model_messages_format(mock_vision_config: DMRConfig) -> None:
    with patch(
        "agents.services.controller_element_finder.send_chat_completion"
    ) as mock_send:
        mock_send.return_value = DMRResponse(
            message=ChatMessage(role="assistant", content="100,200"),
            finish_reason="stop",
            usage_prompt_tokens=100,
            usage_completion_tokens=5,
        )

        _query_vision_model("base64data", "test button", mock_vision_config)

        call_args = mock_send.call_args
        messages = call_args[0][1]

        # Check system message
        assert messages[0].role == "system"
        assert "UI element locator" in messages[0].content
        assert "x,y" in messages[0].content
        assert "NOT_FOUND" in messages[0].content
        assert "AMBIGUOUS" in messages[0].content

        # Check user message
        assert messages[1].role == "user"
        content_parts = messages[1].content
        assert isinstance(content_parts, tuple)
        assert len(content_parts) == 2

        # Check text content
        from agents.types import TextContent

        text_part = content_parts[0]
        assert isinstance(text_part, TextContent)
        assert "Find the element: test button" in text_part.text

        # Check image content
        from agents.types import ImageContent

        image_part = content_parts[1]
        assert isinstance(image_part, ImageContent)
        assert image_part.base64_data == "base64data"


def test_find_element_coordinates_exception_handling(
    mock_vision_config: DMRConfig,
) -> None:
    with patch(
        "agents.services.controller_element_finder.is_omniparser_configured"
    ) as mock_is_omni, patch(
        "agents.services.controller_element_finder.controller_screenshot"
    ) as mock_screenshot:
        mock_is_omni.return_value = False
        mock_screenshot.side_effect = RuntimeError("Screenshot failed")

        with pytest.raises(RuntimeError) as exc_info:
            find_element_coordinates(
                1, "element", mock_vision_config, on_screenshot=None
            )

        assert "Screenshot failed" in str(exc_info.value)
