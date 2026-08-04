from __future__ import annotations

import logging

import httpx
from django.conf import settings

from agents.services.llm_serializer import (
    _parse_response,
    _serialize_messages,
    _serialize_tools,
)
from agents.types import (
    ChatMessage,
    LLMConfig,
    LLMResponse,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


def _build_headers(config: LLMConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.api_key}"}


def _build_payload(
    config: LLMConfig,
    messages: tuple[ChatMessage, ...],
    tools: tuple[ToolDefinition, ...],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": config.model,
        "messages": _serialize_messages(messages),
        "temperature": config.temperature,
        "max_completion_tokens": config.max_tokens,
    }

    if tools:
        payload["tools"] = _serialize_tools(tools)
        payload["tool_choice"] = "auto"

    return payload


def send_chat_completion(
    config: LLMConfig,
    messages: tuple[ChatMessage, ...],
    tools: tuple[ToolDefinition, ...] = (),
) -> LLMResponse:
    headers = _build_headers(config)
    timeout = float(settings.OPENAI_REQUEST_TIMEOUT)
    payload = _build_payload(config, messages, tools)

    logger.info(
        "LLM request: model=%s messages=%d tools=%d",
        config.model,
        len(messages),
        len(tools),
    )

    with httpx.Client(timeout=timeout) as client:
        response = client.post(config.endpoint_url, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.error("LLM error %d: %s", response.status_code, response.text)
        response.raise_for_status()

    data = response.json()
    parsed = _parse_response(data)

    has_tool_calls = parsed.message.tool_calls is not None
    tool_call_count = len(parsed.message.tool_calls) if parsed.message.tool_calls else 0
    logger.info(
        "LLM response: finish_reason=%s tool_calls=%s (count=%d) "
        "usage(prompt=%d, completion=%d)",
        parsed.finish_reason,
        has_tool_calls,
        tool_call_count,
        parsed.usage_prompt_tokens,
        parsed.usage_completion_tokens,
    )

    return parsed
