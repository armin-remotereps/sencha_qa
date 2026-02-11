from __future__ import annotations

from django.test import RequestFactory, TestCase, override_settings

from omniparser.decorators import require_api_key


def _dummy_view(request: object) -> object:
    from django.http import JsonResponse

    return JsonResponse({"ok": True})


decorated_view = require_api_key(_dummy_view)


class RequireApiKeyTest(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    @override_settings(OMNIPARSER_API_KEY="test-secret-key")
    def test_valid_api_key_passes(self) -> None:
        request = self.factory.get("/", HTTP_X_API_KEY="test-secret-key")
        response = decorated_view(request)
        assert response.status_code == 200

    @override_settings(OMNIPARSER_API_KEY="test-secret-key")
    def test_missing_api_key_returns_401(self) -> None:
        request = self.factory.get("/")
        response = decorated_view(request)
        assert response.status_code == 401

    @override_settings(OMNIPARSER_API_KEY="test-secret-key")
    def test_wrong_api_key_returns_401(self) -> None:
        request = self.factory.get("/", HTTP_X_API_KEY="wrong-key")
        response = decorated_view(request)
        assert response.status_code == 401

    @override_settings(OMNIPARSER_API_KEY="")
    def test_empty_configured_key_returns_401(self) -> None:
        request = self.factory.get("/", HTTP_X_API_KEY="")
        response = decorated_view(request)
        assert response.status_code == 401
