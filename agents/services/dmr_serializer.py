from __future__ import annotations

import json

from agents.types import (
    ChatMessage,
    ContentPart,
    DMRResponse,
    ImageContent,
    MessageDict,
    TextContent,
    ToolCall,
    ToolDefinition,
    ToolSchema,
)


def _serialize_messages(messages: tuple[ChatMessage, ...]) -> list[MessageDict]:
    result: list[MessageDict] = []
    for msg in messages:
        serialized = _serialize_single_message(msg)
        result.append(serialized)
    return result


def _serialize_single_message(msg: ChatMessage) -> MessageDict:
    serialized_message: MessageDict = {"role": msg.role}

    if msg.content is not None:
        serialized_message["content"] = _serialize_content(msg.content)

    if msg.tool_calls is not None:
        serialized_message["tool_calls"] = [
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
        serialized_message["tool_call_id"] = msg.tool_call_id

    return serialized_message


def _serialize_content(
    content: str | tuple[ContentPart, ...],
) -> str | list[MessageDict]:
    if isinstance(content, str):
        return content
    return _serialize_multimodal_content(content)


def _serialize_multimodal_content(
    parts: tuple[ContentPart, ...],
) -> list[MessageDict]:
    result: list[MessageDict] = []
    for part in parts:
        if isinstance(part, TextContent):
            result.append({"type": "text", "text": part.text})
        elif isinstance(part, ImageContent):
            result.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{part.media_type};base64,{part.base64_data}",
                    },
                }
            )
    return result


def _serialize_tools(
    tools: tuple[ToolDefinition, ...],
) -> list[ToolSchema]:
    result: list[ToolSchema] = []
    for tool in tools:
        properties: dict[str, object] = {}
        required: list[str] = []

        for param in tool.parameters:
            parameter_schema: dict[str, str | list[str]] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum is not None:
                parameter_schema["enum"] = list(param.enum)
            properties[param.name] = parameter_schema

            if param.required:
                required.append(param.name)

        func_schema: ToolSchema = {
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
