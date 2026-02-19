from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from omniparser_service.dependencies import get_parser_service
from omniparser_service.main import app
from omniparser_service.types import ParseResult


class TestRequireApiKey:
    def test_valid_api_key_passes(self) -> None:
        mock_service = MagicMock()
        mock_service.parse.return_value = ParseResult(
            annotated_image="", elements=(), image_width=100, image_height=100
        )
        app.dependency_overrides[get_parser_service] = lambda: mock_service
        try:
            with patch("omniparser_service.dependencies.settings") as mock_settings:
                mock_settings.api_key = "test-secret-key"
                client = TestClient(app)
                response = client.post(
                    "/omniparser/parse/",
                    json={"image_base64": "abc"},
                    headers={"X-API-Key": "test-secret-key"},
                )
                assert response.status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_missing_api_key_returns_401(self) -> None:
        with patch("omniparser_service.dependencies.settings") as mock_settings:
            mock_settings.api_key = "test-secret-key"
            client = TestClient(app)
            response = client.post(
                "/omniparser/parse/",
                json={"image_base64": "abc"},
            )
            assert response.status_code == 401

    def test_wrong_api_key_returns_401(self) -> None:
        with patch("omniparser_service.dependencies.settings") as mock_settings:
            mock_settings.api_key = "test-secret-key"
            client = TestClient(app)
            response = client.post(
                "/omniparser/parse/",
                json={"image_base64": "abc"},
                headers={"X-API-Key": "wrong-key"},
            )
            assert response.status_code == 401

    def test_empty_configured_key_returns_401(self) -> None:
        with patch("omniparser_service.dependencies.settings") as mock_settings:
            mock_settings.api_key = ""
            client = TestClient(app)
            response = client.post(
                "/omniparser/parse/",
                json={"image_base64": "abc"},
                headers={"X-API-Key": ""},
            )
            assert response.status_code == 401
