from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.exceptions import VncElementNotFoundError
from agents.services.vnc_omniparser_element_finder import (
    _build_element_list,
    _parse_match_response,
    find_element_coordinates_omniparser,
)
from agents.types import ChatMessage, DMRConfig, DMRResponse, ImageContent, TextContent
from omniparser.types import PixelBBox, PixelParseResult, PixelUIElement


@pytest.fixture
def mock_vnc_session() -> MagicMock:
    mock = MagicMock()
    mock.capture_screen.return_value = b"\x89PNG\r\n\x1a\nfake_image_data"
    return mock


@pytest.fixture
def dmr_config() -> DMRConfig:
    return DMRConfig(
        host="test",
        port="8080",
        model="test-model",
        temperature=0.0,
        max_tokens=1000,
    )


@pytest.fixture
def sample_elements() -> tuple[PixelUIElement, ...]:
    return (
        PixelUIElement(
            index=0,
            type="button",
            content="OK",
            bbox=PixelBBox(x_min=10, y_min=20, x_max=100, y_max=50),
            center_x=55,
            center_y=35,
            interactivity=True,
        ),
        PixelUIElement(
            index=1,
            type="text",
            content="Cancel",
            bbox=PixelBBox(x_min=110, y_min=20, x_max=200, y_max=50),
            center_x=155,
            center_y=35,
            interactivity=True,
        ),
        PixelUIElement(
            index=2,
            type="label",
            content="Are you sure?",
            bbox=PixelBBox(x_min=10, y_min=0, x_max=200, y_max=18),
            center_x=105,
            center_y=9,
            interactivity=False,
        ),
    )


def test_build_element_list_formats_correctly(
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    result = _build_element_list(sample_elements)
    lines = result.split("\n")

    assert len(lines) == 3
    assert "[0]" in lines[0]
    assert 'content="OK"' in lines[0]
    assert "[1]" in lines[1]
    assert 'content="Cancel"' in lines[1]
    assert "[2]" in lines[2]
    assert "interactive=False" in lines[2]


def test_build_element_list_empty() -> None:
    result = _build_element_list(())
    assert result == ""


def test_parse_match_response_valid_index(
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    result = _parse_match_response("1", "Cancel button", sample_elements)
    assert result.index == 1
    assert result.content == "Cancel"


def test_parse_match_response_index_in_text(
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    result = _parse_match_response(
        "The matching element is 0", "OK button", sample_elements
    )
    assert result.index == 0


def test_parse_match_response_not_found(
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    with pytest.raises(VncElementNotFoundError, match="No OmniParser element"):
        _parse_match_response("NOT_FOUND", "missing element", sample_elements)


def test_parse_match_response_unparseable(
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    with pytest.raises(VncElementNotFoundError, match="Could not parse"):
        _parse_match_response("I don't know", "button", sample_elements)


def test_parse_match_response_invalid_index(
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    with pytest.raises(VncElementNotFoundError, match="does not match"):
        _parse_match_response("99", "button", sample_elements)


@patch("agents.services.vnc_omniparser_element_finder.send_chat_completion")
@patch("agents.services.vnc_omniparser_element_finder.parse_screenshot_remote")
def test_find_element_coordinates_omniparser_success(
    mock_parse: MagicMock,
    mock_send: MagicMock,
    mock_vnc_session: MagicMock,
    dmr_config: DMRConfig,
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    mock_parse.return_value = PixelParseResult(
        annotated_image="img",
        elements=sample_elements,
        image_width=1920,
        image_height=1080,
    )
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="0"),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    x, y = find_element_coordinates_omniparser(
        mock_vnc_session, "OK button", dmr_config
    )

    assert x == 55
    assert y == 35
    mock_vnc_session.capture_screen.assert_called_once()
    mock_parse.assert_called_once()
    mock_send.assert_called_once()


@patch("agents.services.vnc_omniparser_element_finder.parse_screenshot_remote")
def test_find_element_coordinates_omniparser_no_elements(
    mock_parse: MagicMock,
    mock_vnc_session: MagicMock,
    dmr_config: DMRConfig,
) -> None:
    mock_parse.return_value = PixelParseResult(
        annotated_image="",
        elements=(),
        image_width=1920,
        image_height=1080,
    )

    with pytest.raises(VncElementNotFoundError, match="no UI elements"):
        find_element_coordinates_omniparser(mock_vnc_session, "some button", dmr_config)


@patch("agents.services.vnc_omniparser_element_finder.send_chat_completion")
@patch("agents.services.vnc_omniparser_element_finder.parse_screenshot_remote")
def test_find_element_coordinates_omniparser_dmr_returns_not_found(
    mock_parse: MagicMock,
    mock_send: MagicMock,
    mock_vnc_session: MagicMock,
    dmr_config: DMRConfig,
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    mock_parse.return_value = PixelParseResult(
        annotated_image="img",
        elements=sample_elements,
        image_width=1920,
        image_height=1080,
    )
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="NOT_FOUND"),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    with pytest.raises(VncElementNotFoundError, match="No OmniParser element"):
        find_element_coordinates_omniparser(
            mock_vnc_session, "invisible widget", dmr_config
        )


@patch("agents.services.vnc_omniparser_element_finder.send_chat_completion")
@patch("agents.services.vnc_omniparser_element_finder.parse_screenshot_remote")
def test_find_element_coordinates_omniparser_dmr_returns_none(
    mock_parse: MagicMock,
    mock_send: MagicMock,
    mock_vnc_session: MagicMock,
    dmr_config: DMRConfig,
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    mock_parse.return_value = PixelParseResult(
        annotated_image="img",
        elements=sample_elements,
        image_width=1920,
        image_height=1080,
    )
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content=None),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    with pytest.raises(VncElementNotFoundError, match="empty content"):
        find_element_coordinates_omniparser(
            mock_vnc_session, "some element", dmr_config
        )


@patch("agents.services.vnc_omniparser_element_finder.send_chat_completion")
@patch("agents.services.vnc_omniparser_element_finder.parse_screenshot_remote")
def test_find_element_coordinates_omniparser_sends_annotated_image(
    mock_parse: MagicMock,
    mock_send: MagicMock,
    mock_vnc_session: MagicMock,
    dmr_config: DMRConfig,
    sample_elements: tuple[PixelUIElement, ...],
) -> None:
    mock_parse.return_value = PixelParseResult(
        annotated_image="annotated_base64_data",
        elements=sample_elements,
        image_width=1920,
        image_height=1080,
    )
    mock_send.return_value = DMRResponse(
        message=ChatMessage(role="assistant", content="1"),
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=10,
    )

    find_element_coordinates_omniparser(mock_vnc_session, "Cancel button", dmr_config)

    call_args = mock_send.call_args
    messages = call_args[0][1]
    user_msg = messages[1]
    assert isinstance(user_msg.content, tuple)
    assert len(user_msg.content) == 2
    assert isinstance(user_msg.content[0], ImageContent)
    assert user_msg.content[0].base64_data == "annotated_base64_data"
    assert isinstance(user_msg.content[1], TextContent)
    assert "Cancel button" in user_msg.content[1].text
