from __future__ import annotations

import logging
from typing import Any

from duckduckgo_search import DDGS

from agents.services.tool_utils import safe_tool_call
from agents.types import ToolResult

logger = logging.getLogger(__name__)

_MAX_RESULTS = 5
_MAX_SNIPPET_LENGTH = 300


def web_search(*, query: str) -> ToolResult:
    return safe_tool_call(
        "web_search",
        lambda: _execute_search(query),
    )


def _execute_search(query: str) -> ToolResult:
    results: list[dict[str, Any]] = DDGS().text(query, max_results=_MAX_RESULTS)

    if not results:
        return ToolResult(
            tool_call_id="",
            content="No results found.",
            is_error=False,
        )

    lines: list[str] = []
    for result in results:
        title = result.get("title", "")
        snippet = result.get("body", "")
        url = result.get("href", "")
        if len(snippet) > _MAX_SNIPPET_LENGTH:
            snippet = snippet[:_MAX_SNIPPET_LENGTH] + "..."
        lines.append(f"- {title}\n  {snippet}\n  {url}")

    return ToolResult(
        tool_call_id="",
        content="\n\n".join(lines),
        is_error=False,
    )
