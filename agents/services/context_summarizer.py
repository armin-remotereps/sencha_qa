from __future__ import annotations

import logging

import httpx
from django.conf import settings

from agents.types import (
    ChatMessage,
    ContentPart,
    DMRConfig,
    ImageContent,
    TextContent,
    ToolCall,
)

logger = logging.getLogger(__name__)

_CONTEXT_HEAD_RATIO = 0.75
_SUMMARIZER_MAX_RESPONSE_TOKENS = 1024
_CONTEXT_SUMMARY_PREFIX = "[Context Summary]"
_MIN_MIDDLE_MESSAGES = 2

_BASE_CONTEXT_INSTRUCTIONS = (
    "You are a concise conversation summarizer for an automated test agent. "
    "Your summary MUST preserve:\n"
    "1. Actions taken and their results (success/failure)\n"
    "2. Current state of the task\n"
    "3. Important details: file paths, error messages, version numbers\n"
    "4. Any pending or incomplete steps\n\n"
    "Keep the summary focused and actionable.\n\n"
)


def summarize_context_if_needed(
    messages: list[ChatMessage],
    *,
    summarizer_config: DMRConfig | None = None,
) -> list[ChatMessage]:
    threshold = int(settings.CONTEXT_SUMMARIZE_THRESHOLD)
    size = _estimate_context_size(messages)
    if size <= threshold:
        return messages

    preserve_count = int(settings.CONTEXT_PRESERVE_LAST_MESSAGES)
    prefix, middle, tail = _split_messages(messages, preserve_count)

    if len(middle) < _MIN_MIDDLE_MESSAGES:
        return messages

    logger.info(
        "[Context] Size %d > %d threshold, summarizing %d middle messages",
        size,
        threshold,
        len(middle),
    )

    summary_text = _summarize_middle(
        middle,
        summarizer_config=summarizer_config,
    )

    summary_message = ChatMessage(
        role="user",
        content=f"{_CONTEXT_SUMMARY_PREFIX} {summary_text}",
    )

    return list(prefix) + [summary_message] + list(tail)


def _estimate_context_size(messages: list[ChatMessage]) -> int:
    total = 0
    for msg in messages:
        total += _estimate_message_size(msg)
    return total


def _estimate_message_size(message: ChatMessage) -> int:
    size = 0
    content = message.content
    if content is None:
        size = 0
    elif isinstance(content, str):
        size = len(content)
    elif isinstance(content, tuple):
        for part in content:
            size += _estimate_content_part_size(part)

    if message.tool_calls is not None:
        for tc in message.tool_calls:
            size += len(tc.tool_name) + len(str(tc.arguments))

    return size


def _estimate_content_part_size(part: ContentPart) -> int:
    if isinstance(part, TextContent):
        return len(part.text)
    if isinstance(part, ImageContent):
        return len(part.base64_data)
    return 0


def _split_messages(
    messages: list[ChatMessage],
    preserve_count: int,
) -> tuple[list[ChatMessage], list[ChatMessage], list[ChatMessage]]:
    prefix = list(messages[:2])

    tail_start = max(2, len(messages) - preserve_count)

    # Adjust boundary to avoid orphaning tool results from their assistant message
    while tail_start > 2 and messages[tail_start].role == "tool":
        tail_start -= 1

    # If the message at tail_start-1 is an assistant with tool_calls,
    # pull it into the tail so it stays with its tool results
    if (
        tail_start > 2
        and messages[tail_start - 1].role == "assistant"
        and messages[tail_start - 1].tool_calls is not None
    ):
        tail_start -= 1

    middle = list(messages[2:tail_start])
    tail = list(messages[tail_start:])

    return prefix, middle, tail


def _summarize_middle(
    middle: list[ChatMessage],
    *,
    summarizer_config: DMRConfig | None,
) -> str:
    serialized = _serialize_messages_for_summary(middle)
    return _summarize_with_fallback(
        serialized,
        summarizer_config=summarizer_config,
    )


def _serialize_messages_for_summary(messages: list[ChatMessage]) -> str:
    lines: list[str] = []
    for msg in messages:
        role_tag = msg.role.upper()

        if msg.role == "assistant" and msg.tool_calls is not None:
            for tc in msg.tool_calls:
                lines.append(f"[TOOL_CALL] {tc.tool_name}({tc.arguments})")
            if msg.content is not None:
                content_text = _content_to_text(msg.content)
                if content_text:
                    lines.append(f"[ASSISTANT] {content_text}")
        elif msg.role == "tool":
            content_text = _content_to_text(msg.content)
            lines.append(f"[TOOL_RESULT] {content_text}")
        else:
            content_text = _content_to_text(msg.content)
            lines.append(f"[{role_tag}] {content_text}")

    return "\n".join(lines)


def _content_to_text(content: str | tuple[ContentPart, ...] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, TextContent):
            parts.append(part.text)
        elif isinstance(part, ImageContent):
            parts.append("[IMAGE]")
    return " ".join(parts)


def _summarize_with_fallback(
    text: str,
    *,
    summarizer_config: DMRConfig | None,
) -> str:
    threshold = int(settings.CONTEXT_SUMMARIZE_THRESHOLD)
    if summarizer_config is not None:
        try:
            result = _route_summarization(text, config=summarizer_config)
            logger.info("[Context] AI summary produced (%d chars)", len(result))
            return result
        except Exception:
            logger.warning(
                "[Context] AI summarization failed, falling back to truncation",
                exc_info=True,
            )

    logger.info("[Context] Using truncation fallback (75/25 split)")
    return _truncate_context(text, max_length=threshold)


def _route_summarization(text: str, *, config: DMRConfig) -> str:
    chunk_size = int(settings.CONTEXT_SUMMARIZE_CHUNK_SIZE)
    chunks = _split_into_chunks(text, chunk_size)

    if len(chunks) == 1:
        return _summarize_single(chunks[0], config=config)

    logger.info("[Context] Map-reduce: %d chunks", len(chunks))
    chunk_summaries = _map_summarize(chunks, config=config)
    return _reduce_summaries(chunk_summaries, config=config)


def _split_into_chunks(text: str, chunk_size: int) -> list[str]:
    chunks: list[str] = []
    for i in range(0, len(text), chunk_size):
        chunks.append(text[i : i + chunk_size])
    return chunks


def _summarize_single(text: str, *, config: DMRConfig) -> str:
    prompt = _build_chunk_prompt(text)
    timeout = float(settings.DMR_REQUEST_TIMEOUT)
    with httpx.Client(timeout=timeout) as client:
        return _call_dmr(prompt, config=config, client=client)


def _map_summarize(chunks: list[str], *, config: DMRConfig) -> list[str]:
    summaries: list[str] = []
    timeout = float(settings.DMR_REQUEST_TIMEOUT)
    with httpx.Client(timeout=timeout) as client:
        for idx, chunk in enumerate(chunks):
            logger.info("[Context] Summarizing chunk %d/%d", idx + 1, len(chunks))
            prompt = _build_chunk_prompt(
                chunk,
                chunk_label=f"Chunk {idx + 1}/{len(chunks)}",
            )
            summary = _call_dmr(prompt, config=config, client=client)
            summaries.append(summary)
    return summaries


def _reduce_summaries(summaries: list[str], *, config: DMRConfig) -> str:
    prompt = _build_reduce_prompt(summaries)
    timeout = float(settings.DMR_REQUEST_TIMEOUT)
    with httpx.Client(timeout=timeout) as client:
        return _call_dmr(prompt, config=config, client=client)


def _build_chunk_prompt(text: str, *, chunk_label: str = "") -> str:
    prompt = _BASE_CONTEXT_INSTRUCTIONS
    prompt += "Summarize the following conversation history.\n\n"
    if chunk_label:
        prompt += f"Note: This is {chunk_label} of a larger conversation.\n\n"
    prompt += f"Conversation:\n{text}"
    return prompt


def _build_reduce_prompt(summaries: list[str]) -> str:
    numbered = "\n".join(f"[Chunk {i + 1}] {s}" for i, s in enumerate(summaries))
    prompt = _BASE_CONTEXT_INSTRUCTIONS
    prompt += (
        "Below are summaries of consecutive chunks of a conversation. "
        "Merge them into ONE final summary.\n\n"
    )
    prompt += f"Chunk summaries:\n{numbered}"
    return prompt


def _call_dmr(prompt: str, *, config: DMRConfig, client: httpx.Client) -> str:
    url = f"http://{config.host}:{config.port}/engines/llama.cpp/v1/chat/completions"
    payload: dict[str, object] = {
        "model": config.model,
        "messages": [
            {
                "role": "system",
                "content": "You summarize conversation history concisely.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": _SUMMARIZER_MAX_RESPONSE_TOKENS,
    }

    response = client.post(url, json=payload)
    response.raise_for_status()
    return _extract_content(response.json())


def _extract_content(data: object) -> str:
    if not isinstance(data, dict):
        msg = "Unexpected response format"
        raise ValueError(msg)

    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) == 0:
        msg = "No choices in summarizer response"
        raise ValueError(msg)

    choice = choices[0]
    if not isinstance(choice, dict):
        msg = "Invalid summarizer response format"
        raise ValueError(msg)

    message = choice.get("message")
    if not isinstance(message, dict):
        msg = "No message in summarizer response"
        raise ValueError(msg)

    content = message.get("content")
    if not isinstance(content, str):
        msg = "Summarizer returned non-string content"
        raise ValueError(msg)

    return content.strip()


def _truncate_context(text: str, *, max_length: int) -> str:
    head_size = int(max_length * _CONTEXT_HEAD_RATIO)
    tail_size = max_length - head_size
    return text[:head_size] + "\n\n... [context truncated] ...\n\n" + text[-tail_size:]
