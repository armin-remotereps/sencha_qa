from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from agents.services.tools_search import (
    AI_SUMMARY_HEADER,
    MAX_SNIPPET_LENGTH,
    NO_RESULTS_MESSAGE,
    RAW_RESULTS_HEADER,
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
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = response
    return mock_client


class TestWebSearchFormatted:
    @patch("agents.services.tools_search.httpx.Client")
    def test_returns_formatted_results(self, mock_client_cls: MagicMock) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )

        result = web_search(query="install jdk 17 macOS")

        assert result.is_error is False
        assert "Install JDK 17" in result.content
        assert "brew install openjdk@17" in result.content
        assert "https://example.com/jdk" in result.content
        assert "JDK Downloads" in result.content

    @patch("agents.services.tools_search.httpx.Client")
    def test_no_results(self, mock_client_cls: MagicMock) -> None:
        _setup_mock_http_client(mock_client_cls, _make_searxng_response([]))

        result = web_search(query="xyznonexistentquery12345")

        assert result.is_error is False
        assert NO_RESULTS_MESSAGE in result.content

    @patch("agents.services.tools_search.httpx.Client")
    def test_exception(self, mock_client_cls: MagicMock) -> None:
        mock_client = _setup_mock_http_client(
            mock_client_cls, _make_searxng_response([])
        )
        mock_client.get.side_effect = RuntimeError("network error")

        result = web_search(query="test query")

        assert result.is_error is True
        assert "error" in result.content.lower()

    @patch("agents.services.tools_search.httpx.Client")
    def test_truncates_long_snippets(self, mock_client_cls: MagicMock) -> None:
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

        result = web_search(query="test")

        assert result.is_error is False
        assert "..." in result.content
        lines = result.content.split("\n")
        snippet_line = lines[1].strip()
        assert len(snippet_line) <= MAX_SNIPPET_LENGTH + 3 + 2


class TestWebSearchWithSummarizer:
    @patch("agents.services.tools_search.send_chat_completion")
    @patch("agents.services.tools_search.httpx.Client")
    def test_returns_summary_and_raw(
        self, mock_client_cls: MagicMock, mock_send: MagicMock
    ) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )
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
    @patch("agents.services.tools_search.httpx.Client")
    def test_falls_back_on_summarizer_failure(
        self, mock_client_cls: MagicMock, mock_send: MagicMock
    ) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )
        mock_send.side_effect = RuntimeError("DMR unavailable")

        result = web_search(
            query="install jdk 17 macOS", summarizer_config=_summarizer_config()
        )

        assert result.is_error is False
        assert AI_SUMMARY_HEADER not in result.content
        assert "Install JDK 17" in result.content

    @patch("agents.services.tools_search.httpx.Client")
    def test_without_summarizer_config(self, mock_client_cls: MagicMock) -> None:
        _setup_mock_http_client(
            mock_client_cls, _make_searxng_response(_sample_results())
        )

        result = web_search(query="install jdk 17 macOS", summarizer_config=None)

        assert result.is_error is False
        assert AI_SUMMARY_HEADER not in result.content
        assert "Install JDK 17" in result.content
