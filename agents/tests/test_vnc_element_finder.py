from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.vnc_element_finder import (
    VncElementNotFoundError,
    _parse_coordinates,
    find_element_coordinates,
)
from agents.types import ChatMessage, DMRConfig, DMRResponse


@pytest.fixture
def mock_vnc_session() -> MagicMock:
    mock = MagicMock()
    mock.capture_screen.return_value = b"\x89PNG\r\n\x1a\nfake_image_data"
    return mock


@pytest.fixture
def vision_config() -> DMRConfig:
    return DMRConfig(
        host="test",
        port="8080",
        model="test-vision",
        temperature=0.0,
        max_tokens=1000,
    )


def test_parse_coordinates_comma_separated_integers() -> None:
    result = _parse_coordinates("450,320", "button")
    assert result == (450, 320)


def test_parse_coordinates_with_spaces() -> None:
    result = _parse_coordinates("450 , 320", "button")
    assert result == (450, 320)


def test_parse_coordinates_embedded_in_text() -> None:
    result = _parse_coordinates(
        "The button is located at 450,320 in the center", "button"
    )
    assert result == (450, 320)


def test_parse_coordinates_raises_on_not_found() -> None:
    with pytest.raises(VncElementNotFoundError, match="not found on screen"):
        _parse_coordinates("NOT_FOUND", "submit button")


def test_parse_coordinates_raises_on_ambiguous() -> None:
    with pytest.raises(VncElementNotFoundError, match="Ambiguous"):
        _parse_coordinates("AMBIGUOUS: two buttons match", "button")


def test_parse_coordinates_raises_on_unparseable() -> None:
    with pytest.raises(VncElementNotFoundError, match="Could not parse"):
        _parse_coordinates("I don't know", "button")


@patch("agents.services.vnc_element_finder.is_omniparser_configured", return_value=True)
@patch(
    "agents.services.vnc_element_finder.find_element_coordinates_omniparser",
    return_value=(100, 200),
)
def test_find_element_delegates_to_omniparser_when_configured(
    mock_omniparser_find: MagicMock,
    mock_is_configured: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    x, y = find_element_coordinates(mock_vnc_session, "OK button", vision_config)

    assert (x, y) == (100, 200)
    mock_is_configured.assert_called_once()
    mock_omniparser_find.assert_called_once_with(
        mock_vnc_session, "OK button", vision_config, on_screenshot=None
    )
    mock_vnc_session.capture_screen.assert_not_called()


@patch("agents.services.vnc_element_finder.send_chat_completion")
@patch(
    "agents.services.vnc_element_finder.is_omniparser_configured", return_value=False
)
def test_find_element_falls_back_to_vision_when_not_configured(
    mock_is_configured: MagicMock,
    mock_send: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="300,400"),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    x, y = find_element_coordinates(mock_vnc_session, "button", vision_config)

    assert (x, y) == (300, 400)
    mock_is_configured.assert_called_once()
    mock_vnc_session.capture_screen.assert_called_once()
    mock_send.assert_called_once()


@patch(
    "agents.services.vnc_element_finder.is_omniparser_configured", return_value=False
)
@patch("agents.services.vnc_element_finder.send_chat_completion")
def test_find_element_coordinates_returns_parsed_coordinates(
    mock_send: MagicMock,
    _mock_is_configured: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="250,180"),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    x, y = find_element_coordinates(mock_vnc_session, "the OK button", vision_config)

    assert x == 250
    assert y == 180
    mock_vnc_session.capture_screen.assert_called_once()
    mock_send.assert_called_once()


@patch(
    "agents.services.vnc_element_finder.is_omniparser_configured", return_value=False
)
@patch("agents.services.vnc_element_finder.send_chat_completion")
def test_find_element_coordinates_raises_on_not_found(
    mock_send: MagicMock,
    _mock_is_configured: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="NOT_FOUND"),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    with pytest.raises(VncElementNotFoundError, match="not found on screen"):
        find_element_coordinates(mock_vnc_session, "invisible button", vision_config)


@patch(
    "agents.services.vnc_element_finder.is_omniparser_configured", return_value=False
)
@patch("agents.services.vnc_element_finder.send_chat_completion")
def test_find_element_coordinates_raises_on_non_string_response(
    mock_send: MagicMock,
    _mock_is_configured: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content=None),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    with pytest.raises(VncElementNotFoundError, match="no response"):
        find_element_coordinates(mock_vnc_session, "some element", vision_config)


@patch(
    "agents.services.vnc_element_finder.is_omniparser_configured", return_value=False
)
@patch("agents.services.vnc_element_finder.send_chat_completion")
def test_find_element_coordinates_sends_image_to_vision(
    mock_send: MagicMock,
    _mock_is_configured: MagicMock,
    mock_vnc_session: MagicMock,
    vision_config: DMRConfig,
) -> None:
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="100,200"),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    find_element_coordinates(mock_vnc_session, "test button", vision_config)

    call_args = mock_send.call_args
    messages = call_args[0][1]
    user_msg = messages[1]
    assert isinstance(user_msg.content, tuple)
    assert len(user_msg.content) == 2
