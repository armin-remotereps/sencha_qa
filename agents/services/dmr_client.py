from __future__ import annotations

import json
import logging
import subprocess

import httpx
from django.conf import settings

from agents.types import (
    ChatMessage,
    ContentPart,
    DMRConfig,
    DMRResponse,
    ImageContent,
    TextContent,
    ToolCall,
    ToolDefinition,
)

logger = logging.getLogger(__name__)

MODEL_PULL_TIMEOUT = 600  # 10 minutes for large model downloads
_DOCKER_IO_PREFIX = "docker.io/"


def _normalize_model_id(model_id: str) -> str:
    """Strip the 'docker.io/' prefix that DMR API adds to model IDs."""
    if model_id.startswith(_DOCKER_IO_PREFIX):
        return model_id[len(_DOCKER_IO_PREFIX) :]
    return model_id


def build_dmr_config(*, model: str | None = None) -> DMRConfig:
    """Build DMRConfig from Django settings, optionally overriding model."""
    return DMRConfig(
        host=settings.DMR_HOST,
        port=settings.DMR_PORT,
        model=model or settings.DMR_MODEL,
        temperature=settings.DMR_TEMPERATURE,
        max_tokens=settings.DMR_MAX_TOKENS,
    )


def build_summarizer_config(*, model: str | None = None) -> DMRConfig:
    """Build DMRConfig for the output summarizer model."""
    return DMRConfig(
        host=settings.DMR_HOST,
        port=settings.DMR_PORT,
        model=model or settings.DMR_SUMMARIZER_MODEL,
        temperature=0.0,
        max_tokens=512,
    )


def build_vision_dmr_config(*, model: str | None = None) -> DMRConfig:
    """Build DMRConfig for the vision model from Django settings."""
    return DMRConfig(
        host=settings.DMR_HOST,
        port=settings.DMR_PORT,
        model=model or settings.DMR_VISION_MODEL,
        temperature=settings.DMR_TEMPERATURE,
        max_tokens=settings.DMR_MAX_TOKENS,
    )


def list_models(config: DMRConfig) -> list[str]:
    """List available models from DMR."""
    url = f"http://{config.host}:{config.port}/engines/llama.cpp/v1/models"
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url)
        response.raise_for_status()
    data = response.json()
    models: list[str] = []
    raw_data = data.get("data")
    if isinstance(raw_data, list):
        for item in raw_data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str):
                    models.append(_normalize_model_id(model_id))
    return models


def is_model_available(config: DMRConfig) -> bool:
    """Check if the configured model is available in DMR."""
    try:
        models = list_models(config)
        return config.model in models
    except Exception as e:
        logger.debug("Failed to check model availability: %s", e)
        return False


def pull_model(model: str) -> None:
    """Pull a model using docker model pull."""
    logger.info("Pulling model: %s (this may take a while)...", model)
    result = subprocess.run(
        ["docker", "model", "pull", model],
        capture_output=True,
        text=True,
        timeout=MODEL_PULL_TIMEOUT,
    )
    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip()
        msg = f"Failed to pull model {model}: {error_msg}"
        raise RuntimeError(msg)
    logger.info("Model pulled successfully: %s", model)


def _is_remote_host(host: str) -> bool:
    """Return True if the host is not the local machine."""
    return host not in ("localhost", "127.0.0.1", "::1")


def ensure_model_available(config: DMRConfig) -> None:
    """Check if model is available, pull it if not.

    For remote DMR hosts, logs a warning instead of attempting a local pull.
    """
    if is_model_available(config):
        logger.debug("Model already available: %s", config.model)
        return
    if _is_remote_host(config.host):
        logger.warning(
            "Model %s not found on remote host %s:%s. "
            "Please ensure the model is installed on the remote DMR instance.",
            config.model,
            config.host,
            config.port,
        )
        return
    logger.info("Model not available: %s. Pulling...", config.model)
    pull_model(config.model)


def send_chat_completion(
    config: DMRConfig,
    messages: tuple[ChatMessage, ...],
    tools: tuple[ToolDefinition, ...] = (),
) -> DMRResponse:
    """Send a chat completion request to DMR and return parsed response."""
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

    logger.debug("DMR request to %s with %d messages", url, len(messages))

    with httpx.Client(timeout=float(settings.DMR_REQUEST_TIMEOUT)) as client:
        response = client.post(url, json=payload)
        if response.status_code >= 400:
            logger.error("DMR error %d: %s", response.status_code, response.text)
        response.raise_for_status()

    data = response.json()
    return _parse_response(data)


def _serialize_messages(messages: tuple[ChatMessage, ...]) -> list[dict[str, object]]:
    """Serialize ChatMessage objects to dicts for the API."""
    result: list[dict[str, object]] = []
    for msg in messages:
        serialized = _serialize_single_message(msg)
        result.append(serialized)
    return result


def _serialize_single_message(msg: ChatMessage) -> dict[str, object]:
    """Serialize a single ChatMessage."""
    d: dict[str, object] = {"role": msg.role}

    if msg.content is not None:
        d["content"] = _serialize_content(msg.content)

    if msg.tool_calls is not None:
        d["tool_calls"] = [
            {
                "id": tc.tool_call_id,
                "type": "function",
                "function": {
                    "name": tc.tool_name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in msg.tool_calls
        ]

    if msg.tool_call_id is not None:
        d["tool_call_id"] = msg.tool_call_id

    return d


def _serialize_content(
    content: str | tuple[ContentPart, ...],
) -> str | list[dict[str, object]]:
    """Serialize message content, handling multimodal."""
    if isinstance(content, str):
        return content

    parts: list[dict[str, object]] = []
    for part in content:
        if isinstance(part, TextContent):
            parts.append({"type": "text", "text": part.text})
        elif isinstance(part, ImageContent):
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{part.media_type};base64,{part.base64_data}",
                    },
                }
            )
    return parts


def _serialize_tools(
    tools: tuple[ToolDefinition, ...],
) -> list[dict[str, object]]:
    """Serialize ToolDefinitions to OpenAI-compatible tool schemas."""
    result: list[dict[str, object]] = []
    for tool in tools:
        properties: dict[str, object] = {}
        required: list[str] = []

        for param in tool.parameters:
            prop: dict[str, str | list[str]] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum is not None:
                prop["enum"] = list(param.enum)
            properties[param.name] = prop

            if param.required:
                required.append(param.name)

        func_schema: dict[str, object] = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }
        result.append(func_schema)
    return result


def _parse_response(data: dict[str, object]) -> DMRResponse:
    """Parse the DMR API response JSON into DMRResponse."""
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) == 0:
        msg = "No choices in DMR response"
        raise ValueError(msg)

    choice = choices[0]
    if not isinstance(choice, dict):
        msg = "Invalid choice format"
        raise ValueError(msg)

    finish_reason = str(choice.get("finish_reason", "stop"))
    raw_message = choice.get("message")
    if not isinstance(raw_message, dict):
        msg = "No message in DMR response choice"
        raise ValueError(msg)

    # Parse tool calls if present
    tool_calls = _parse_tool_calls(raw_message)

    raw_content = raw_message.get("content")
    content: str | None = str(raw_content) if raw_content is not None else None

    raw_reasoning = raw_message.get("reasoning_content")
    reasoning: str | None = str(raw_reasoning) if raw_reasoning is not None else None

    message = ChatMessage(
        role=str(raw_message.get("role", "assistant")),
        content=content,
        tool_calls=tool_calls,
    )

    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}

    return DMRResponse(
        message=message,
        reasoning_content=reasoning,
        finish_reason=finish_reason,
        usage_prompt_tokens=int(usage.get("prompt_tokens", 0)),
        usage_completion_tokens=int(usage.get("completion_tokens", 0)),
    )


def _parse_tool_calls(raw_message: dict[str, object]) -> tuple[ToolCall, ...] | None:
    """Parse tool_calls from raw response message."""
    raw_tool_calls = raw_message.get("tool_calls")
    if not isinstance(raw_tool_calls, list) or len(raw_tool_calls) == 0:
        return None

    parsed: list[ToolCall] = []
    for raw_tc in raw_tool_calls:
        if not isinstance(raw_tc, dict):
            continue
        tc_id = str(raw_tc.get("id", ""))
        func = raw_tc.get("function")
        if not isinstance(func, dict):
            continue
        name = str(func.get("name", ""))
        raw_args = func.get("arguments", "{}")
        try:
            arguments: dict[str, object] = json.loads(str(raw_args))
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        parsed.append(ToolCall(tool_call_id=tc_id, tool_name=name, arguments=arguments))

    if not parsed:
        return None
    return tuple(parsed)
