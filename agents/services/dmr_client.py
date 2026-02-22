from __future__ import annotations

import logging

import httpx
from django.conf import settings

from agents.services.dmr_serializer import (
    _parse_response,
    _serialize_messages,
    _serialize_tools,
)
from agents.types import (
    ChatMessage,
    DMRConfig,
    DMRResponse,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


def _is_openai_api(config: DMRConfig) -> bool:
    return config.api_key is not None


def _build_url(config: DMRConfig) -> str:
    if config.base_url is not None:
        return config.base_url
    return f"http://{config.host}:{config.port}/engines/llama.cpp/v1/chat/completions"


def _build_headers(config: DMRConfig) -> dict[str, str]:
    if config.api_key is not None:
        return {"Authorization": f"Bearer {config.api_key}"}
    return {}


def _get_timeout(config: DMRConfig) -> float:
    if _is_openai_api(config):
        return float(settings.OPENAI_REQUEST_TIMEOUT)
    return float(settings.DMR_REQUEST_TIMEOUT)


def _build_payload(
    config: DMRConfig,
    messages: tuple[ChatMessage, ...],
    tools: tuple[ToolDefinition, ...],
    keep_alive: int | None,
) -> dict[str, object]:
    token_key = "max_completion_tokens" if _is_openai_api(config) else "max_tokens"
    payload: dict[str, object] = {
        "model": config.model,
        "messages": _serialize_messages(messages),
        "temperature": config.temperature,
        token_key: config.max_tokens,
    }

    if tools:
        payload["tools"] = _serialize_tools(tools)
        payload["tool_choice"] = "auto"

    if keep_alive is not None and not _is_openai_api(config):
        payload["keep_alive"] = keep_alive

    return payload


def send_chat_completion(
    config: DMRConfig,
    messages: tuple[ChatMessage, ...],
    tools: tuple[ToolDefinition, ...] = (),
    *,
    keep_alive: int | None = None,
) -> DMRResponse:
    url = _build_url(config)
    headers = _build_headers(config)
    timeout = _get_timeout(config)
    payload = _build_payload(config, messages, tools, keep_alive)

    logger.info(
        "DMR request: model=%s messages=%d tools=%d",
        config.model,
        len(messages),
        len(tools),
    )

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.error("DMR error %d: %s", response.status_code, response.text)
        response.raise_for_status()

    data = response.json()
    parsed = _parse_response(data)

    has_tool_calls = parsed.message.tool_calls is not None
    tool_call_count = len(parsed.message.tool_calls) if parsed.message.tool_calls else 0
    logger.info(
        "DMR response: finish_reason=%s tool_calls=%s (count=%d) "
        "usage(prompt=%d, completion=%d)",
        parsed.finish_reason,
        has_tool_calls,
        tool_call_count,
        parsed.usage_prompt_tokens,
        parsed.usage_completion_tokens,
    )

    return parsed
