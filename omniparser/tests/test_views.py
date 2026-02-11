from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

from omniparser.types import (
    BBox,
    ParseResult,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
    UIElement,
)


class HealthViewTest(TestCase):
    def test_health_returns_ok(self) -> None:
        client = Client()
        response = client.get("/omniparser/health/")
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "ok"

    def test_health_rejects_post(self) -> None:
        client = Client()
        response = client.post("/omniparser/health/")
        assert response.status_code == 405


class ReadyViewTest(TestCase):
    @patch("omniparser.views.OmniParserService")
    def test_ready_returns_models_loaded(self, mock_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.models_loaded = True
        mock_cls.return_value = mock_instance

        client = Client()
        response = client.get("/omniparser/ready/")
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["models_loaded"] is True

    @patch("omniparser.views.OmniParserService")
    def test_ready_returns_false_when_not_loaded(self, mock_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.models_loaded = False
        mock_cls.return_value = mock_instance

        client = Client()
        response = client.get("/omniparser/ready/")
        data = json.loads(response.content)
        assert data["models_loaded"] is False


@override_settings(OMNIPARSER_API_KEY="test-key")
class ParseScreenshotViewTest(TestCase):
    def setUp(self) -> None:
        self.client = Client()
        self.url = "/omniparser/parse/"
        self.headers = {"HTTP_X_API_KEY": "test-key"}

    def test_missing_api_key_returns_401(self) -> None:
        response = self.client.post(
            self.url,
            data=json.dumps({"image_base64": "abc"}),
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_wrong_api_key_returns_401(self) -> None:
        response = self.client.post(
            self.url,
            data=json.dumps({"image_base64": "abc"}),
            content_type="application/json",
            HTTP_X_API_KEY="wrong",
        )
        assert response.status_code == 401

    def test_invalid_json_returns_400(self) -> None:
        response = self.client.post(
            self.url,
            data="not json",
            content_type="application/json",
            **self.headers,  # type: ignore[arg-type]
        )
        assert response.status_code == 400

    def test_missing_image_base64_returns_400(self) -> None:
        response = self.client.post(
            self.url,
            data=json.dumps({}),
            content_type="application/json",
            **self.headers,  # type: ignore[arg-type]
        )
        assert response.status_code == 400

    @patch("omniparser.views.OmniParserService")
    def test_successful_parse(self, mock_cls: MagicMock) -> None:
        mock_service = MagicMock()
        mock_cls.return_value = mock_service
        mock_service.parse.return_value = ParseResult(
            annotated_image="annotated_b64",
            elements=(
                UIElement(
                    index=0,
                    type="text",
                    content="OK",
                    bbox=BBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4),
                    center_x=0.2,
                    center_y=0.3,
                    interactivity=False,
                ),
            ),
            image_width=1920,
            image_height=1080,
        )

        response = self.client.post(
            self.url,
            data=json.dumps({"image_base64": "abc123"}),
            content_type="application/json",
            **self.headers,  # type: ignore[arg-type]
        )
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["annotated_image"] == "annotated_b64"
        assert len(data["elements"]) == 1
        assert data["elements"][0]["content"] == "OK"
        assert data["image_width"] == 1920

    @patch("omniparser.views.OmniParserService")
    def test_parse_service_error_returns_500(self, mock_cls: MagicMock) -> None:
        mock_service = MagicMock()
        mock_cls.return_value = mock_service
        mock_service.parse.side_effect = RuntimeError("Model failed")

        response = self.client.post(
            self.url,
            data=json.dumps({"image_base64": "abc123"}),
            content_type="application/json",
            **self.headers,  # type: ignore[arg-type]
        )
        assert response.status_code == 500

    def test_get_method_not_allowed(self) -> None:
        response = self.client.get(self.url, **self.headers)  # type: ignore[arg-type]
        assert response.status_code == 405


@override_settings(OMNIPARSER_API_KEY="test-key")
class ParseScreenshotPixelsViewTest(TestCase):
    def setUp(self) -> None:
        self.client = Client()
        self.url = "/omniparser/parse/pixels/"
        self.headers = {"HTTP_X_API_KEY": "test-key"}

    def test_missing_api_key_returns_401(self) -> None:
        response = self.client.post(
            self.url,
            data=json.dumps({"image_base64": "abc"}),
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_invalid_json_returns_400(self) -> None:
        response = self.client.post(
            self.url,
            data="not json",
            content_type="application/json",
            **self.headers,  # type: ignore[arg-type]
        )
        assert response.status_code == 400

    def test_missing_image_base64_returns_400(self) -> None:
        response = self.client.post(
            self.url,
            data=json.dumps({}),
            content_type="application/json",
            **self.headers,  # type: ignore[arg-type]
        )
        assert response.status_code == 400

    @patch("omniparser.views.OmniParserService")
    def test_successful_parse_pixels(self, mock_cls: MagicMock) -> None:
        mock_service = MagicMock()
        mock_cls.return_value = mock_service
        mock_service.parse_pixels.return_value = PixelParseResult(
            annotated_image="annotated_b64",
            elements=(
                PixelUIElement(
                    index=0,
                    type="icon",
                    content="Close",
                    bbox=PixelBBox(x_min=100, y_min=200, x_max=300, y_max=400),
                    center_x=200,
                    center_y=300,
                    interactivity=True,
                ),
            ),
            image_width=1920,
            image_height=1080,
        )

        response = self.client.post(
            self.url,
            data=json.dumps({"image_base64": "abc123"}),
            content_type="application/json",
            **self.headers,  # type: ignore[arg-type]
        )
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["elements"][0]["bbox"]["x_min"] == 100
        assert data["elements"][0]["center_x"] == 200
        assert data["image_width"] == 1920

    @patch("omniparser.views.OmniParserService")
    def test_parse_pixels_service_error_returns_500(self, mock_cls: MagicMock) -> None:
        mock_service = MagicMock()
        mock_cls.return_value = mock_service
        mock_service.parse_pixels.side_effect = RuntimeError("Boom")

        response = self.client.post(
            self.url,
            data=json.dumps({"image_base64": "abc123"}),
            content_type="application/json",
            **self.headers,  # type: ignore[arg-type]
        )
        assert response.status_code == 500
