from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from controller_client.browser_executor import (
    _DOWNLOAD_TIMEOUT_MS,
    BrowserSession,
    _detect_login_page,
    execute_browser_download,
)
from controller_client.exceptions import ExecutionError
from controller_client.protocol import BrowserDownloadPayload


def _make_mock_page(
    url: str = "https://example.com", title: str = "Example"
) -> MagicMock:
    page = MagicMock()
    page.url = url
    page.title.return_value = title
    return page


class TestDetectLoginPage:
    def test_url_match(self) -> None:
        indicators = [
            "login",
            "log in",
            "sign in",
            "signin",
            "authenticate",
            "authorization",
            "sso",
            "cas/login",
            "oauth",
            "saml",
        ]
        for indicator in indicators:
            page = _make_mock_page(url=f"https://example.com/{indicator}/page")
            result = _detect_login_page(page)
            assert result is not None, f"Should detect '{indicator}' in URL"
            assert result == page.url

    def test_title_match(self) -> None:
        page = _make_mock_page(
            url="https://example.com/auth-page",
            title="Please Sign In to continue",
        )
        result = _detect_login_page(page)
        assert result is not None
        assert result == page.url

    def test_no_match(self) -> None:
        page = _make_mock_page(
            url="https://example.com/downloads/file.zip",
            title="Download Center",
        )
        result = _detect_login_page(page)
        assert result is None

    def test_title_exception(self) -> None:
        page = _make_mock_page(url="https://example.com/normal-page")
        page.title.side_effect = Exception("Page crashed")
        result = _detect_login_page(page)
        assert result is None


class TestExecuteBrowserDownload:
    def test_success(self, tmp_path: Path) -> None:
        save_path = str(tmp_path / "file.zip")
        payload = BrowserDownloadPayload(
            url="https://example.com/file.zip", save_path=save_path
        )
        session = MagicMock(spec=BrowserSession)
        page = MagicMock()
        session.ensure_page.return_value = page

        mock_download = MagicMock()
        mock_download.suggested_filename = "file.zip"

        # Set up expect_download context manager
        download_ctx = MagicMock()
        download_ctx.__enter__ = MagicMock(return_value=download_ctx)
        download_ctx.__exit__ = MagicMock(return_value=False)
        download_ctx.value = mock_download
        page.expect_download.return_value = download_ctx

        # Make save_as actually create the file
        def fake_save_as(path: str) -> None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"fake content")

        mock_download.save_as.side_effect = fake_save_as

        result = execute_browser_download(session, payload)

        assert result.success is True
        assert save_path in result.message
        assert "12 bytes" in result.message

    def test_timeout_login_redirect(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        payload = BrowserDownloadPayload(
            url="https://example.com/file.zip", save_path="/tmp/file.zip"
        )
        session = MagicMock(spec=BrowserSession)
        page = MagicMock()
        session.ensure_page.return_value = page

        page.url = "https://example.com/login?redirect=/file.zip"
        page.title.return_value = "Login Page"

        download_ctx = MagicMock()
        download_ctx.__enter__ = MagicMock(return_value=download_ctx)
        download_ctx.__exit__ = MagicMock(return_value=False)
        page.expect_download.return_value = download_ctx
        page.goto.side_effect = PlaywrightTimeoutError("Timeout 15000ms exceeded")

        with pytest.raises(ExecutionError, match="redirected to login page"):
            execute_browser_download(session, payload)

    def test_timeout_login_in_title(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        payload = BrowserDownloadPayload(
            url="https://example.com/file.zip", save_path="/tmp/file.zip"
        )
        session = MagicMock(spec=BrowserSession)
        page = MagicMock()
        session.ensure_page.return_value = page

        page.url = "https://example.com/auth-gateway"
        page.title.return_value = "Please Sign In"

        download_ctx = MagicMock()
        download_ctx.__enter__ = MagicMock(return_value=download_ctx)
        download_ctx.__exit__ = MagicMock(return_value=False)
        page.expect_download.return_value = download_ctx
        page.goto.side_effect = PlaywrightTimeoutError("Timeout 15000ms exceeded")

        with pytest.raises(ExecutionError, match="redirected to login page"):
            execute_browser_download(session, payload)

    def test_timeout_no_login(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        payload = BrowserDownloadPayload(
            url="https://example.com/file.zip", save_path="/tmp/file.zip"
        )
        session = MagicMock(spec=BrowserSession)
        page = MagicMock()
        session.ensure_page.return_value = page

        page.url = "https://example.com/some-other-page"
        page.title.return_value = "Not Found"

        download_ctx = MagicMock()
        download_ctx.__enter__ = MagicMock(return_value=download_ctx)
        download_ctx.__exit__ = MagicMock(return_value=False)
        page.expect_download.return_value = download_ctx
        page.goto.side_effect = PlaywrightTimeoutError("Timeout 15000ms exceeded")

        with pytest.raises(ExecutionError, match="No download triggered"):
            execute_browser_download(session, payload)

    def test_uses_15s_timeout(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        payload = BrowserDownloadPayload(
            url="https://example.com/file.zip", save_path="/tmp/file.zip"
        )
        session = MagicMock(spec=BrowserSession)
        page = MagicMock()
        session.ensure_page.return_value = page

        page.url = "https://example.com/file.zip"
        page.title.return_value = "Download"

        download_ctx = MagicMock()
        download_ctx.__enter__ = MagicMock(return_value=download_ctx)
        download_ctx.__exit__ = MagicMock(return_value=False)
        page.expect_download.return_value = download_ctx
        page.goto.side_effect = PlaywrightTimeoutError("Timeout")

        with pytest.raises(ExecutionError):
            execute_browser_download(session, payload)

        page.expect_download.assert_called_once_with(timeout=_DOWNLOAD_TIMEOUT_MS)

    def test_generic_exception(self) -> None:
        payload = BrowserDownloadPayload(
            url="https://example.com/file.zip", save_path="/tmp/file.zip"
        )
        session = MagicMock(spec=BrowserSession)
        session.ensure_page.side_effect = RuntimeError("Browser crashed")

        with pytest.raises(ExecutionError, match="Browser download failed"):
            execute_browser_download(session, payload)
