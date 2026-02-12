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


def _build_url(config: DMRConfig) -> str:
    if config.base_url is not None:
        return config.base_url
    return f"http://{config.host}:{config.port}/engines/llama.cpp/v1/chat/completions"


def _build_headers(config: DMRConfig) -> dict[str, str]:
    if config.api_key is not None:
        return {"Authorization": f"Bearer {config.api_key}"}
    return {}


def _get_timeout(config: DMRConfig) -> float:
    if config.api_key is not None:
        return float(settings.OPENAI_REQUEST_TIMEOUT)
    return float(settings.DMR_REQUEST_TIMEOUT)


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

    token_limit_key = (
        "max_completion_tokens" if config.api_key is not None else "max_tokens"
    )
    payload: dict[str, object] = {
        "model": config.model,
        "messages": _serialize_messages(messages),
        "temperature": config.temperature,
        token_limit_key: config.max_tokens,
    }

    if tools:
        payload["tools"] = _serialize_tools(tools)
        payload["tool_choice"] = "auto"

    if keep_alive is not None and config.api_key is None:
        payload["keep_alive"] = keep_alive

    logger.debug("DMR request to %s with %d messages", url, len(messages))

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.error("DMR error %d: %s", response.status_code, response.text)
        response.raise_for_status()

    data = response.json()
    return _parse_response(data)
