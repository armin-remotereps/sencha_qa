from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.services.omniparser_client import (
    OmniParserConnectionError,
    _deserialize_pixel_parse_result,
    is_omniparser_configured,
    parse_screenshot_remote,
)
from omniparser.types import PixelBBox, PixelParseResult


@patch("agents.services.omniparser_client.settings")
def test_is_configured_returns_true_when_url_set(mock_settings: MagicMock) -> None:
    mock_settings.OMNIPARSER_URL = "http://omniparser:8000"
    assert is_omniparser_configured() is True


@patch("agents.services.omniparser_client.settings")
def test_is_configured_returns_false_when_url_empty(mock_settings: MagicMock) -> None:
    mock_settings.OMNIPARSER_URL = ""
    assert is_omniparser_configured() is False


@patch("agents.services.omniparser_client.settings")
def test_is_configured_returns_false_when_url_whitespace(
    mock_settings: MagicMock,
) -> None:
    mock_settings.OMNIPARSER_URL = "   "
    assert is_omniparser_configured() is False


def test_deserialize_pixel_parse_result_with_elements() -> None:
    data: dict[str, object] = {
        "annotated_image": "base64data",
        "image_width": 1920,
        "image_height": 1080,
        "elements": [
            {
                "index": 0,
                "type": "button",
                "content": "OK",
                "bbox": {"x_min": 10, "y_min": 20, "x_max": 100, "y_max": 50},
                "center_x": 55,
                "center_y": 35,
                "interactivity": True,
            },
        ],
    }

    result = _deserialize_pixel_parse_result(data)

    assert isinstance(result, PixelParseResult)
    assert result.image_width == 1920
    assert result.image_height == 1080
    assert result.annotated_image == "base64data"
    assert len(result.elements) == 1
    el = result.elements[0]
    assert el.index == 0
    assert el.type == "button"
    assert el.content == "OK"
    assert el.center_x == 55
    assert el.center_y == 35
    assert el.interactivity is True
    assert el.bbox == PixelBBox(x_min=10, y_min=20, x_max=100, y_max=50)


def test_deserialize_pixel_parse_result_empty_elements() -> None:
    data: dict[str, object] = {
        "annotated_image": "",
        "image_width": 800,
        "image_height": 600,
        "elements": [],
    }

    result = _deserialize_pixel_parse_result(data)

    assert result.elements == ()
    assert result.image_width == 800


@patch("agents.services.omniparser_client.httpx.Client")
@patch("agents.services.omniparser_client.settings")
def test_parse_screenshot_remote_success(
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    mock_settings.OMNIPARSER_URL = "http://omniparser:8000"
    mock_settings.OMNIPARSER_API_KEY = "test-key"
    mock_settings.OMNIPARSER_REQUEST_TIMEOUT = 600

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "annotated_image": "img",
        "image_width": 1920,
        "image_height": 1080,
        "elements": [
            {
                "index": 0,
                "type": "text",
                "content": "Hello",
                "bbox": {"x_min": 0, "y_min": 0, "x_max": 100, "y_max": 50},
                "center_x": 50,
                "center_y": 25,
                "interactivity": False,
            },
        ],
    }

    mock_client_instance = MagicMock()
    mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
    mock_client_instance.__exit__ = MagicMock(return_value=False)
    mock_client_instance.post.return_value = mock_response
    mock_client_cls.return_value = mock_client_instance

    result = parse_screenshot_remote("base64img")

    assert isinstance(result, PixelParseResult)
    assert len(result.elements) == 1
    assert result.elements[0].content == "Hello"

    mock_client_instance.post.assert_called_once_with(
        "http://omniparser:8000/omniparser/parse/pixels/",
        json={"image_base64": "base64img"},
        headers={"X-API-Key": "test-key"},
    )


@patch("agents.services.omniparser_client.httpx.Client")
@patch("agents.services.omniparser_client.settings")
def test_parse_screenshot_remote_raises_on_http_error(
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    mock_settings.OMNIPARSER_URL = "http://omniparser:8000"
    mock_settings.OMNIPARSER_API_KEY = "test-key"
    mock_settings.OMNIPARSER_REQUEST_TIMEOUT = 600

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Server Error",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client_instance = MagicMock()
    mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
    mock_client_instance.__exit__ = MagicMock(return_value=False)
    mock_client_instance.post.return_value = mock_response
    mock_client_cls.return_value = mock_client_instance

    with pytest.raises(OmniParserConnectionError, match="OmniParser request failed"):
        parse_screenshot_remote("base64img")


@patch("agents.services.omniparser_client.httpx.Client")
@patch("agents.services.omniparser_client.settings")
def test_parse_screenshot_remote_raises_on_connection_error(
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    mock_settings.OMNIPARSER_URL = "http://omniparser:8000"
    mock_settings.OMNIPARSER_API_KEY = "test-key"
    mock_settings.OMNIPARSER_REQUEST_TIMEOUT = 600

    mock_client_instance = MagicMock()
    mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
    mock_client_instance.__exit__ = MagicMock(return_value=False)
    mock_client_instance.post.side_effect = httpx.ConnectError("Connection refused")
    mock_client_cls.return_value = mock_client_instance

    with pytest.raises(OmniParserConnectionError, match="OmniParser request failed"):
        parse_screenshot_remote("base64img")


@patch("agents.services.omniparser_client.httpx.Client")
@patch("agents.services.omniparser_client.settings")
def test_parse_screenshot_remote_strips_trailing_slash_from_url(
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    mock_settings.OMNIPARSER_URL = "http://omniparser:8000/"
    mock_settings.OMNIPARSER_API_KEY = "key"
    mock_settings.OMNIPARSER_REQUEST_TIMEOUT = 600

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "annotated_image": "",
        "image_width": 800,
        "image_height": 600,
        "elements": [],
    }

    mock_client_instance = MagicMock()
    mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
    mock_client_instance.__exit__ = MagicMock(return_value=False)
    mock_client_instance.post.return_value = mock_response
    mock_client_cls.return_value = mock_client_instance

    parse_screenshot_remote("img")

    call_url = mock_client_instance.post.call_args[0][0]
    assert call_url == "http://omniparser:8000/omniparser/parse/pixels/"
