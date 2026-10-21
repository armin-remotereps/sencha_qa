from __future__ import annotations

import logging

import httpx
from django.conf import settings

from agents.exceptions import AIServiceError, AIServiceErrorKind
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

# Provider error bodies can be long (or echo the prompt); keep only a short
# prefix in the user-facing message.
_PROVIDER_ERROR_MAX_CHARS = 300


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

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(config.endpoint_url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise AIServiceError(
            AIServiceErrorKind.TIMEOUT,
            f"AI service did not respond within {timeout:.0f} seconds",
        ) from exc
    except httpx.RequestError as exc:
        raise AIServiceError(
            AIServiceErrorKind.CONNECTION,
            f"Could not connect to the AI service ({type(exc).__name__})",
        ) from exc

    if response.status_code >= 400:
        logger.error("LLM error %d: %s", response.status_code, response.text)
        raise _status_error(response)

    try:
        parsed = _parse_response(response.json())
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # json.JSONDecodeError is a ValueError; the rest cover a body whose
        # shape does not match the chat-completions contract.
        raise AIServiceError(
            AIServiceErrorKind.BAD_RESPONSE,
            f"AI service returned an unusable response: {_truncate(str(exc))}",
            status_code=response.status_code,
        ) from exc

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


def _status_error(response: httpx.Response) -> AIServiceError:
    """Map an HTTP error reply to an ``AIServiceError``.

    Built from the status code and body only: ``httpx.HTTPStatusError``'s text
    includes the endpoint URL, which must never reach the UI or TestRail.
    """
    status = response.status_code
    if status == 429:
        kind, label = AIServiceErrorKind.RATE_LIMITED, "rate limited the request"
    elif status in (401, 403):
        kind, label = AIServiceErrorKind.AUTH, "rejected the credentials"
    elif status >= 500:
        kind, label = AIServiceErrorKind.UNAVAILABLE, "is unavailable"
    else:
        kind, label = AIServiceErrorKind.REJECTED, "rejected the request"

    message = f"AI service {label} (HTTP {status})"
    # Auth error bodies can echo part of the API key; they stay in the server log.
    detail = "" if kind == AIServiceErrorKind.AUTH else _truncate(response.text.strip())
    if detail:
        message = f"{message}: {detail}"
    return AIServiceError(kind, message, status_code=status)


def _truncate(text: str) -> str:
    if len(text) <= _PROVIDER_ERROR_MAX_CHARS:
        return text
    return f"{text[:_PROVIDER_ERROR_MAX_CHARS]}…"
