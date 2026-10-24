from __future__ import annotations

import json

from agents.types import (
    ChatMessage,
    ContentPart,
    ImageContent,
    LLMResponse,
    MessageDict,
    ReasoningItem,
    TextContent,
    ToolCall,
    ToolDefinition,
    ToolSchema,
)


def _serialize_input(
    messages: tuple[ChatMessage, ...],
) -> tuple[str | None, list[MessageDict]]:
    """Split a chat history into Responses API ``instructions`` and ``input`` items.

    The leading system message becomes ``instructions``; any later one is sent as
    a developer message.
    """
    instructions: str | None = None
    remaining = messages
    if messages and messages[0].role == "system":
        instructions = _content_text(messages[0].content)
        remaining = messages[1:]

    items: list[MessageDict] = []
    for msg in remaining:
        items.extend(_serialize_single_message(msg))
    return instructions, items


def _serialize_single_message(msg: ChatMessage) -> list[MessageDict]:
    if msg.role == "tool":
        if msg.tool_call_id is None:
            error = "A tool message needs the tool_call_id of the call it answers"
            raise ValueError(error)
        return [
            {
                "type": "function_call_output",
                "call_id": msg.tool_call_id,
                "output": _content_text(msg.content),
            }
        ]
    if msg.role == "assistant":
        return _serialize_assistant_message(msg)

    role = "developer" if msg.role == "system" else msg.role
    content = msg.content if msg.content is not None else ""
    return [{"role": role, "content": _serialize_content(content)}]


def _serialize_assistant_message(msg: ChatMessage) -> list[MessageDict]:
    # Same order the model produced them in: reasoning, text, function calls.
    following: list[MessageDict] = []

    text = _content_text(msg.content)
    if text:
        message_item: MessageDict = {"role": "assistant", "content": text}
        if msg.phase is not None:
            message_item["phase"] = msg.phase
        following.append(message_item)

    for tc in msg.tool_calls or ():
        following.append(
            {
                "type": "function_call",
                "call_id": tc.tool_call_id,
                "name": tc.tool_name,
                "arguments": json.dumps(tc.arguments),
            }
        )

    # The API rejects a reasoning item that has nothing after it.
    if not following:
        return []

    reasoning_items: list[MessageDict] = [
        {
            "type": "reasoning",
            "id": item.item_id,
            "summary": [
                {"type": "summary_text", "text": text} for text in item.summary
            ],
            "encrypted_content": item.encrypted_content,
        }
        for item in msg.reasoning or ()
    ]
    return reasoning_items + following


def _content_text(content: str | tuple[ContentPart, ...] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(part.text for part in content if isinstance(part, TextContent))


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
            result.append({"type": "input_text", "text": part.text})
        elif isinstance(part, ImageContent):
            result.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{part.media_type};base64,{part.base64_data}",
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
            parameter_schema: dict[str, object] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum is not None:
                parameter_schema["enum"] = list(param.enum)
            if param.items_schema is not None:
                parameter_schema["items"] = param.items_schema
            elif param.items_type is not None:
                parameter_schema["items"] = {"type": param.items_type}
            if param.max_items is not None:
                parameter_schema["maxItems"] = param.max_items
            properties[param.name] = parameter_schema

            if param.required:
                required.append(param.name)

        func_schema: ToolSchema = {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
            # Strict mode would rewrite our schemas (optional params, no
            # additionalProperties); keep the arguments the model sends as-is.
            "strict": False,
        }
        result.append(func_schema)
    return result


def _parse_response(data: dict[str, object]) -> LLMResponse:
    status = str(data.get("status", "completed"))
    error = data.get("error")
    if status in ("failed", "cancelled") or isinstance(error, dict):
        detail = _error_detail(error)
        msg = f"Response {status}{f': {detail}' if detail else ''}"
        raise ValueError(msg)

    output = data.get("output")
    if not isinstance(output, list):
        msg = "No output in LLM response"
        raise ValueError(msg)

    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    reasoning: list[ReasoningItem] = []
    phases: set[str | None] = set()

    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "reasoning":
            parsed_reasoning = _parse_reasoning_item(item)
            if parsed_reasoning is not None:
                reasoning.append(parsed_reasoning)
        elif item_type == "message":
            texts.append("".join(_parse_message_texts(item)))
            raw_phase = item.get("phase")
            phases.add(raw_phase if isinstance(raw_phase, str) else None)
        elif item_type == "function_call":
            tool_calls.append(_parse_function_call(item))

    message = ChatMessage(
        role="assistant",
        content="\n\n".join(text for text in texts if text) or None,
        tool_calls=tuple(tool_calls) or None,
        reasoning=tuple(reasoning) or None,
        # Several message items are replayed as one, so a phase is kept only
        # when they all share it; resending a wrong phase is worse than none.
        phase=phases.pop() if len(phases) == 1 else None,
    )

    summaries = [text for item in reasoning for text in item.summary]

    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}

    return LLMResponse(
        message=message,
        reasoning_content="\n\n".join(summaries) or None,
        finish_reason=_finish_reason(
            status, data.get("incomplete_details"), bool(tool_calls)
        ),
        usage_prompt_tokens=int(usage.get("input_tokens", 0)),
        usage_completion_tokens=int(usage.get("output_tokens", 0)),
        usage_reasoning_tokens=int(output_details.get("reasoning_tokens", 0)),
    )


def _error_detail(error: object) -> str:
    if not isinstance(error, dict):
        return ""
    parts = [str(error[key]) for key in ("code", "message") if error.get(key)]
    return " ".join(parts)


def _finish_reason(
    status: str, incomplete_details: object, has_tool_calls: bool
) -> str:
    """Map the Responses status to the Chat Completions finish_reason vocabulary."""
    if status == "incomplete":
        reason = (
            str(incomplete_details.get("reason", ""))
            if isinstance(incomplete_details, dict)
            else ""
        )
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "content_filter"
        return f"incomplete:{reason}"
    if has_tool_calls:
        return "tool_calls"
    return "stop"


def _parse_reasoning_item(item: dict[str, object]) -> ReasoningItem | None:
    item_id = item.get("id")
    encrypted = item.get("encrypted_content")
    if not isinstance(item_id, str) or not isinstance(encrypted, str):
        return None

    summary: list[str] = []
    raw_summary = item.get("summary")
    if isinstance(raw_summary, list):
        for part in raw_summary:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                summary.append(str(part["text"]))
    return ReasoningItem(
        item_id=item_id, encrypted_content=encrypted, summary=tuple(summary)
    )


def _parse_message_texts(item: dict[str, object]) -> list[str]:
    content = item.get("content")
    if not isinstance(content, list):
        return []

    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        # A refusal is surfaced as text so callers see why nothing was done.
        if part.get("type") == "output_text" and isinstance(part.get("text"), str):
            texts.append(str(part["text"]))
        elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
            texts.append(str(part["refusal"]))
    return texts


def _parse_function_call(item: dict[str, object]) -> ToolCall:
    raw_args = item.get("arguments", "{}")
    try:
        arguments: dict[str, object] = json.loads(str(raw_args))
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    return ToolCall(
        tool_call_id=str(item.get("call_id", "")),
        tool_name=str(item.get("name", "")),
        arguments=arguments,
    )
