from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from django.conf import settings
from django.test import override_settings

from agents.services.tools_search import (
    AI_SUMMARY_HEADER,
    MAX_SNIPPET_LENGTH,
    NO_RESULTS_MESSAGE,
    RAW_RESULTS_HEADER,
    _fetch_page_content,
    _fetch_pages_content,
    _format_results,
    web_search,
)
from agents.types import ChatMessage, DMRConfig, DMRResponse


def _make_searxng_response(results: list[dict[str, Any]]) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"results": results}
    mock_response.raise_for_status.return_value = None
    return mock_response


def _sample_results() -> list[dict[str, Any]]:
    return [
        {
            "title": "Install JDK 17",
            "content": "Use brew install openjdk@17",
            "url": "https://example.com/jdk",
        },
        {
            "title": "JDK Downloads",
            "content": "Official Oracle downloads page",
            "url": "https://oracle.com/jdk",
        },
    ]


def _summarizer_config() -> DMRConfig:
    return DMRConfig(
        host="localhost",
        port="12434",
        model="ai/mistral",
        temperature=0.0,
        max_tokens=512,
    )


def _setup_mock_http_client(
    mock_client_cls: MagicMock, response: MagicMock
) -> MagicMock:
    """Configure mock to behave as an httpx.Client context manager."""
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = response
    return mock_client


class TestFetchPageContent:
    @patch("agents.services.tools_search.trafilatura.extract")
    @patch("agents.services.tools_search.httpx.Client")
    def test_extracts_content(
        self, mock_client_cls: MagicMock, mock_extract: MagicMock
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = "<html><body><p>Hello world</p></body></html>"
        mock_response.raise_for_status.return_value = None
        _setup_mock_http_client(mock_client_cls, mock_response)
        mock_extract.return_value = "Hello world"

        result = _fetch_page_content("https://example.com")

        assert result == "Hello world"
        mock_extract.assert_called_once_with(mock_response.text)

    @override_settings(SEARCH_PAGE_MAX_LENGTH=100)
    @patch("agents.services.tools_search.trafilatura.extract")
    @patch("agents.services.tools_search.httpx.Client")
    def test_truncates_long_content(
        self, mock_client_cls: MagicMock, mock_extract: MagicMock
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = "<html><body>long</body></html>"
        mock_response.raise_for_status.return_value = None
        _setup_mock_http_client(mock_client_cls, mock_response)
        mock_extract.return_value = "x" * 200

        result = _fetch_page_content("https://example.com")

        assert result.endswith("...")
        assert len(result) == settings.SEARCH_PAGE_MAX_LENGTH + 3

    @patch("agents.services.tools_search.trafilatura.extract")
    @patch("agents.services.tools_search.httpx.Client")
    def test_returns_empty_on_no_extraction(
        self, mock_client_cls: MagicMock, mock_extract: MagicMock
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = "<html></html>"
        mock_response.raise_for_status.return_value = None
        _setup_mock_http_client(mock_client_cls, mock_response)
        mock_extract.return_value = None

        result = _fetch_page_content("https://example.com")

        assert result == ""

    @patch("agents.services.tools_search.httpx.Client")
    def test_returns_empty_on_http_error(self, mock_client_cls: MagicMock) -> None:
        mock_client = _setup_mock_http_client(mock_client_cls, MagicMock())
        mock_client.get.side_effect = httpx.HTTPError("timeout")

        result = _fetch_page_content("https://example.com")

        assert result == ""


class TestFetchPagesContent:
    @patch("agents.services.tools_search._fetch_page_content")
    def test_fetches_all_passed_urls(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = "content"
        urls = ["https://a.com", "https://b.com", "https://c.com"]

        result = _fetch_pages_content(urls)

        assert len(result) == 3
        assert mock_fetch.call_count == 3

    @patch("agents.services.tools_search._fetch_page_content")
    def test_handles_partial_failures(self, mock_fetch: MagicMock) -> None:
        content_map = {
            "https://a.com": "content1",
            "https://b.com": "",
            "https://c.com": "content3",
        }
        mock_fetch.side_effect = lambda url: content_map[url]
        urls = ["https://a.com", "https://b.com", "https://c.com"]

        result = _fetch_pages_content(urls)

        assert result["https://a.com"] == "content1"
        assert result["https://b.com"] == ""
        assert result["https://c.com"] == "content3"

    @patch("agents.services.tools_search._fetch_page_content")
    def test_empty_urls(self, mock_fetch: MagicMock) -> None:
        result = _fetch_pages_content([])

        assert result == {}
        mock_fetch.assert_not_called()


class TestFormatResults:
    def test_with_page_content(self) -> None:
        results = _sample_results()
        page_contents = {"https://example.com/jdk": "Full page about JDK 17 install"}

        formatted = _format_results(results, page_contents)

        assert "Full page about JDK 17 install" in formatted
        assert "Official Oracle downloads page" in formatted

    def test_falls_back_to_snippet(self) -> None:
        results = _sample_results()
        page_contents: dict[str, str] = {"https://example.com/jdk": ""}

        formatted = _format_results(results, page_contents)

        assert "Use brew install openjdk@17" in formatted

    def test_with_empty_page_contents(self) -> None:
        results = _sample_results()

        formatted = _format_results(results, {})

        assert "Use brew install openjdk@17" in formatted
        assert "Official Oracle downloads page" in formatted

    def test_truncates_long_snippets_without_page_content(self) -> None:
        results = [
            {
                "title": "Long",
                "content": "x" * 500,
                "url": "https://example.com",
            }
        ]

        formatted = _format_results(results, {})

        assert "..." in formatted


class TestWebSearchFormatted:
    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_returns_formatted_results(
        self, mock_client_cls: MagicMock, mock_fetch_pages: MagicMock
    ) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )
        mock_fetch_pages.return_value = {
            "https://example.com/jdk": "Detailed JDK 17 install guide",
        }

        result = web_search(query="install jdk 17 macOS")

        assert result.is_error is False
        assert "Install JDK 17" in result.content
        assert "Detailed JDK 17 install guide" in result.content

    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_no_results(
        self, mock_client_cls: MagicMock, mock_fetch_pages: MagicMock
    ) -> None:
        _setup_mock_http_client(mock_client_cls, _make_searxng_response([]))

        result = web_search(query="xyznonexistentquery12345")

        assert result.is_error is False
        assert NO_RESULTS_MESSAGE in result.content
        mock_fetch_pages.assert_not_called()

    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_exception(
        self, mock_client_cls: MagicMock, mock_fetch_pages: MagicMock
    ) -> None:
        mock_client = _setup_mock_http_client(
            mock_client_cls, _make_searxng_response([])
        )
        mock_client.get.side_effect = RuntimeError("network error")

        result = web_search(query="test query")

        assert result.is_error is True
        assert "error" in result.content.lower()

    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_truncates_long_snippets(
        self, mock_client_cls: MagicMock, mock_fetch_pages: MagicMock
    ) -> None:
        long_content = "x" * 500
        _setup_mock_http_client(
            mock_client_cls,
            _make_searxng_response(
                [
                    {
                        "title": "Long Result",
                        "content": long_content,
                        "url": "https://example.com",
                    },
                ]
            ),
        )
        mock_fetch_pages.return_value = {"https://example.com": ""}

        result = web_search(query="test")

        assert result.is_error is False
        assert "..." in result.content
        lines = result.content.split("\n")
        snippet_line = lines[2].strip()
        assert len(snippet_line) <= MAX_SNIPPET_LENGTH + 3 + 2

    @override_settings(SEARCH_FETCH_PAGE_COUNT=2)
    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_slices_urls_to_fetch_count(
        self, mock_client_cls: MagicMock, mock_fetch_pages: MagicMock
    ) -> None:
        results = [
            {"title": f"R{i}", "content": f"s{i}", "url": f"https://{i}.com"}
            for i in range(5)
        ]
        _setup_mock_http_client(mock_client_cls, _make_searxng_response(results))
        mock_fetch_pages.return_value = {}

        web_search(query="test")

        fetched_urls = mock_fetch_pages.call_args[0][0]
        assert len(fetched_urls) == 2


class TestWebSearchWithSummarizer:
    @patch("agents.services.tools_search.send_chat_completion")
    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_returns_summary_and_raw(
        self,
        mock_client_cls: MagicMock,
        mock_fetch_pages: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )
        mock_fetch_pages.return_value = {}
        mock_send.return_value = DMRResponse(
            message=ChatMessage(
                role="assistant",
                content="Run `brew install openjdk@17` on macOS.",
            ),
            finish_reason="stop",
            usage_prompt_tokens=100,
            usage_completion_tokens=20,
        )

        result = web_search(
            query="install jdk 17 macOS", summarizer_config=_summarizer_config()
        )

        assert result.is_error is False
        assert AI_SUMMARY_HEADER in result.content
        assert "brew install openjdk@17" in result.content
        assert RAW_RESULTS_HEADER in result.content
        assert "https://example.com/jdk" in result.content
        mock_send.assert_called_once()

    @patch("agents.services.tools_search.send_chat_completion")
    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_falls_back_on_summarizer_failure(
        self,
        mock_client_cls: MagicMock,
        mock_fetch_pages: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )
        mock_fetch_pages.return_value = {}
        mock_send.side_effect = RuntimeError("DMR unavailable")

        result = web_search(
            query="install jdk 17 macOS", summarizer_config=_summarizer_config()
        )

        assert result.is_error is False
        assert AI_SUMMARY_HEADER not in result.content
        assert "Install JDK 17" in result.content

    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_without_summarizer_config(
        self, mock_client_cls: MagicMock, mock_fetch_pages: MagicMock
    ) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )
        mock_fetch_pages.return_value = {}

        result = web_search(query="install jdk 17 macOS", summarizer_config=None)

        assert result.is_error is False
        assert AI_SUMMARY_HEADER not in result.content
        assert "Install JDK 17" in result.content


class TestPageContentEnrichment:
    @patch("agents.services.tools_search.send_chat_completion")
    @patch("agents.services.tools_search._fetch_pages_content")
    @patch("agents.services.tools_search.httpx.Client")
    def test_page_content_reaches_summarizer(
        self,
        mock_client_cls: MagicMock,
        mock_fetch_pages: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )
        mock_fetch_pages.return_value = {
            "https://example.com/jdk": "Step 1: sudo apt install openjdk-17-jdk",
        }
        mock_send.return_value = DMRResponse(
            message=ChatMessage(role="assistant", content="Install via apt."),
            finish_reason="stop",
            usage_prompt_tokens=100,
            usage_completion_tokens=20,
        )

        web_search(query="install jdk 17", summarizer_config=_summarizer_config())

        call_args = mock_send.call_args
        messages = call_args[0][1]
        user_message = messages[1].content
        assert "sudo apt install openjdk-17-jdk" in user_message
