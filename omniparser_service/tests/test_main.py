from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from omniparser_service.dependencies import get_parser_service, require_api_key
from omniparser_service.main import app
from omniparser_service.types import (
    BBox,
    ParseResult,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
    UIElement,
)


def _no_api_key_check() -> None:
    pass


class TestHealthEndpoint:
    def test_health_returns_ok(self, client: TestClient) -> None:
        response = client.get("/omniparser/health/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_rejects_post(self, client: TestClient) -> None:
        response = client.post("/omniparser/health/")
        assert response.status_code == 405


class TestReadyEndpoint:
    def test_ready_returns_models_loaded(self, client: TestClient) -> None:
        mock_service = MagicMock()
        mock_service.models_loaded = True
        app.dependency_overrides[get_parser_service] = lambda: mock_service
        try:
            response = client.get("/omniparser/ready/")
            assert response.status_code == 200
            assert response.json()["models_loaded"] is True
        finally:
            app.dependency_overrides.pop(get_parser_service, None)

    def test_ready_returns_false_when_not_loaded(self, client: TestClient) -> None:
        mock_service = MagicMock()
        mock_service.models_loaded = False
        app.dependency_overrides[get_parser_service] = lambda: mock_service
        try:
            response = client.get("/omniparser/ready/")
            assert response.json()["models_loaded"] is False
        finally:
            app.dependency_overrides.pop(get_parser_service, None)


class TestParseEndpoint:
    def test_missing_api_key_returns_401(self, client: TestClient) -> None:
        response = client.post(
            "/omniparser/parse/",
            json={"image_base64": "abc"},
        )
        assert response.status_code == 401

    def test_wrong_api_key_returns_401(self, client: TestClient) -> None:
        with patch("omniparser_service.dependencies.settings") as mock_settings:
            mock_settings.api_key = "test-key"
            response = client.post(
                "/omniparser/parse/",
                json={"image_base64": "abc"},
                headers={"X-API-Key": "wrong"},
            )
            assert response.status_code == 401

    def test_missing_image_base64_returns_422(self, client: TestClient) -> None:
        app.dependency_overrides[require_api_key] = _no_api_key_check
        try:
            response = client.post(
                "/omniparser/parse/",
                json={},
                headers={"X-API-Key": "test-key"},
            )
            assert response.status_code == 422
        finally:
            app.dependency_overrides.pop(require_api_key, None)

    def test_successful_parse(self, client: TestClient) -> None:
        mock_service = MagicMock()
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
        app.dependency_overrides[get_parser_service] = lambda: mock_service
        app.dependency_overrides[require_api_key] = _no_api_key_check
        try:
            response = client.post(
                "/omniparser/parse/",
                json={"image_base64": "abc123"},
                headers={"X-API-Key": "test-key"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["annotated_image"] == "annotated_b64"
            assert len(data["elements"]) == 1
            assert data["elements"][0]["content"] == "OK"
            assert data["image_width"] == 1920
        finally:
            app.dependency_overrides.clear()

    def test_parse_service_error_returns_500(self, client: TestClient) -> None:
        mock_service = MagicMock()
        mock_service.parse.side_effect = RuntimeError("Model failed")
        app.dependency_overrides[get_parser_service] = lambda: mock_service
        app.dependency_overrides[require_api_key] = _no_api_key_check
        try:
            response = client.post(
                "/omniparser/parse/",
                json={"image_base64": "abc123"},
                headers={"X-API-Key": "test-key"},
            )
            assert response.status_code == 500
        finally:
            app.dependency_overrides.clear()

    def test_get_method_not_allowed(self, client: TestClient) -> None:
        response = client.get(
            "/omniparser/parse/",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 405


class TestParsePixelsEndpoint:
    def test_missing_api_key_returns_401(self, client: TestClient) -> None:
        response = client.post(
            "/omniparser/parse/pixels/",
            json={"image_base64": "abc"},
        )
        assert response.status_code == 401

    def test_missing_image_base64_returns_422(self, client: TestClient) -> None:
        app.dependency_overrides[require_api_key] = _no_api_key_check
        try:
            response = client.post(
                "/omniparser/parse/pixels/",
                json={},
                headers={"X-API-Key": "test-key"},
            )
            assert response.status_code == 422
        finally:
            app.dependency_overrides.pop(require_api_key, None)

    def test_successful_parse_pixels(self, client: TestClient) -> None:
        mock_service = MagicMock()
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
        app.dependency_overrides[get_parser_service] = lambda: mock_service
        app.dependency_overrides[require_api_key] = _no_api_key_check
        try:
            response = client.post(
                "/omniparser/parse/pixels/",
                json={"image_base64": "abc123"},
                headers={"X-API-Key": "test-key"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["elements"][0]["bbox"]["x_min"] == 100
            assert data["elements"][0]["center_x"] == 200
            assert data["image_width"] == 1920
        finally:
            app.dependency_overrides.clear()

    def test_parse_pixels_service_error_returns_500(self, client: TestClient) -> None:
        mock_service = MagicMock()
        mock_service.parse_pixels.side_effect = RuntimeError("Boom")
        app.dependency_overrides[get_parser_service] = lambda: mock_service
        app.dependency_overrides[require_api_key] = _no_api_key_check
        try:
            response = client.post(
                "/omniparser/parse/pixels/",
                json={"image_base64": "abc123"},
                headers={"X-API-Key": "test-key"},
            )
            assert response.status_code == 500
        finally:
            app.dependency_overrides.clear()
