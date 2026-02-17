from __future__ import annotations

import logging
from typing import Any

import httpx
from django.conf import settings

from agents.services.dmr_client import send_chat_completion
from agents.services.tool_utils import safe_tool_call
from agents.types import ChatMessage, DMRConfig, ToolResult

logger = logging.getLogger(__name__)

MAX_SNIPPET_LENGTH = 300
NO_RESULTS_MESSAGE = "No results found."
AI_SUMMARY_HEADER = "[AI Summary]"
RAW_RESULTS_HEADER = "[Raw Results]"

_SUMMARIZER_SYSTEM_PROMPT = (
    "You are a concise search result summarizer for an automated QA test agent. "
    "Given a search query and raw search results, produce a short, direct answer "
    "that the agent can act on immediately. Focus on actionable steps, commands, "
    "or key facts. Do not add disclaimers or hedging."
)


def web_search(*, query: str, summarizer_config: DMRConfig | None = None) -> ToolResult:
    """Execute a web search via SearXNG and optionally summarize with AI.

    When summarizer_config is provided the result contains both an AI summary
    and the raw search results.  If summarization fails, raw results are
    returned as a graceful fallback.
    """
    return safe_tool_call(
        "web_search",
        lambda: _execute_search(query, summarizer_config),
    )


def _fetch_searxng_results(query: str) -> list[dict[str, Any]]:
    base_url: str = settings.SEARXNG_BASE_URL
    max_results: int = settings.SEARXNG_MAX_RESULTS
    timeout: int = settings.SEARXNG_REQUEST_TIMEOUT

    params: dict[str, str | int] = {
        "q": query,
        "format": "json",
        "categories": "general",
    }

    with httpx.Client(timeout=timeout) as client:
        response = client.get(f"{base_url}/search", params=params)
        response.raise_for_status()

    data: dict[str, Any] = response.json()
    results: list[dict[str, Any]] = data.get("results", [])
    return results[:max_results]


def _format_results(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for result in results:
        title = result.get("title", "")
        snippet = result.get("content", "")
        url = result.get("url", "")
        if len(snippet) > MAX_SNIPPET_LENGTH:
            snippet = snippet[:MAX_SNIPPET_LENGTH] + "..."
        lines.append(f"- {title}\n  {snippet}\n  {url}")
    return "\n\n".join(lines)


def _summarize_results(
    query: str,
    formatted_results: str,
    config: DMRConfig,
) -> str:
    user_prompt = f"Search query: {query}\n\nSearch results:\n{formatted_results}"
    messages = (
        ChatMessage(role="system", content=_SUMMARIZER_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_prompt),
    )
    response = send_chat_completion(config, messages)
    content = response.message.content
    if isinstance(content, str):
        return content
    return ""


def _build_result(content: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=content, is_error=False)


def _attempt_summarization(query: str, formatted: str, config: DMRConfig) -> ToolResult:
    try:
        summary = _summarize_results(query, formatted, config)
        combined = (
            f"{AI_SUMMARY_HEADER}\n{summary}\n\n{RAW_RESULTS_HEADER}\n{formatted}"
        )
        return _build_result(combined)
    except (httpx.HTTPError, ValueError, TypeError, RuntimeError):
        logger.warning(
            "AI summarization failed for query '%s', returning raw results",
            query,
            exc_info=True,
        )
        return _build_result(formatted)


def _execute_search(query: str, summarizer_config: DMRConfig | None) -> ToolResult:
    results = _fetch_searxng_results(query)

    if not results:
        return _build_result(NO_RESULTS_MESSAGE)

    formatted = _format_results(results)

    if summarizer_config is None:
        return _build_result(formatted)

    return _attempt_summarization(query, formatted, summarizer_config)
