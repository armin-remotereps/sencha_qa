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


def send_chat_completion(
    config: DMRConfig,
    messages: tuple[ChatMessage, ...],
    tools: tuple[ToolDefinition, ...] = (),
    *,
    keep_alive: int | None = None,
) -> DMRResponse:
    url = f"http://{config.host}:{config.port}/engines/llama.cpp/v1/chat/completions"

    payload: dict[str, object] = {
        "model": config.model,
        "messages": _serialize_messages(messages),
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }

    if tools:
        payload["tools"] = _serialize_tools(tools)
        payload["tool_choice"] = "auto"

    if keep_alive is not None:
        payload["keep_alive"] = keep_alive

    logger.debug("DMR request to %s with %d messages", url, len(messages))

    with httpx.Client(timeout=float(settings.DMR_REQUEST_TIMEOUT)) as client:
        response = client.post(url, json=payload)
        if response.status_code >= 400:
            logger.error("DMR error %d: %s", response.status_code, response.text)
        response.raise_for_status()

    data = response.json()
    return _parse_response(data)
