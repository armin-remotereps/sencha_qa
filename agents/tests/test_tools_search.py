from __future__ import annotations

from unittest.mock import MagicMock, patch

from agents.services.tools_search import _MAX_SNIPPET_LENGTH, web_search


class TestWebSearch:
    @patch("agents.services.tools_search.DDGS")
    def test_returns_formatted_results(self, mock_ddgs_cls: MagicMock) -> None:
        mock_ddgs_cls.return_value.text.return_value = [
            {
                "title": "Install JDK 17",
                "body": "Use brew install openjdk@17",
                "href": "https://example.com/jdk",
            },
            {
                "title": "JDK Downloads",
                "body": "Official Oracle downloads page",
                "href": "https://oracle.com/jdk",
            },
        ]

        result = web_search(query="install jdk 17 macOS")

        assert result.is_error is False
        assert "Install JDK 17" in result.content
        assert "brew install openjdk@17" in result.content
        assert "https://example.com/jdk" in result.content
        assert "JDK Downloads" in result.content
        mock_ddgs_cls.return_value.text.assert_called_once_with(
            "install jdk 17 macOS", max_results=5
        )

    @patch("agents.services.tools_search.DDGS")
    def test_no_results(self, mock_ddgs_cls: MagicMock) -> None:
        mock_ddgs_cls.return_value.text.return_value = []

        result = web_search(query="xyznonexistentquery12345")

        assert result.is_error is False
        assert "No results found" in result.content

    @patch("agents.services.tools_search.DDGS")
    def test_exception(self, mock_ddgs_cls: MagicMock) -> None:
        mock_ddgs_cls.return_value.text.side_effect = RuntimeError("network error")

        result = web_search(query="test query")

        assert result.is_error is True
        assert "error" in result.content.lower()

    @patch("agents.services.tools_search.DDGS")
    def test_truncates_long_snippets(self, mock_ddgs_cls: MagicMock) -> None:
        long_body = "x" * 500
        mock_ddgs_cls.return_value.text.return_value = [
            {
                "title": "Long Result",
                "body": long_body,
                "href": "https://example.com",
            },
        ]

        result = web_search(query="test")

        assert result.is_error is False
        # Should be truncated to _MAX_SNIPPET_LENGTH + "..."
        assert "..." in result.content
        # The snippet in the output should not contain the full 500 chars
        lines = result.content.split("\n")
        snippet_line = lines[1].strip()
        assert (
            len(snippet_line) <= _MAX_SNIPPET_LENGTH + 3 + 2
        )  # +3 for "..." +2 for indent
