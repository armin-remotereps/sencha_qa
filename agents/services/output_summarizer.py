from __future__ import annotations

import logging

from django.conf import settings

from agents.services.dmr_client import send_chat_completion
from agents.types import ChatMessage, DMRConfig

logger = logging.getLogger(__name__)

OUTPUT_HEAD_RATIO = 0.75

_BASE_SUMMARY_INSTRUCTIONS = (
    "You are a concise output summarizer for an automated test agent. "
    "Your summary MUST include:\n"
    "1. status: SUCCESS or FAILED\n"
    "2. reason: one-line explanation of what happened\n"
    "3. key_info: preserve any critical error messages, file paths, "
    "version numbers, or actionable details\n\n"
    "Keep the summary under 800 characters.\n\n"
)


def summarize_output(
    output: str,
    *,
    tool_name: str = "",
    is_error: bool = False,
    summarizer_config: DMRConfig | None = None,
) -> str:
    threshold: int = int(settings.OUTPUT_SUMMARIZE_THRESHOLD)
    if len(output) <= threshold:
        return output

    logger.info(
        "[Summarizer] Output exceeds threshold (%d > %d chars), summarizing...",
        len(output),
        threshold,
    )
    return _summarize_with_fallback(
        output,
        tool_name=tool_name,
        is_error=is_error,
        summarizer_config=summarizer_config,
        threshold=threshold,
    )


def _summarize_with_fallback(
    output: str,
    *,
    tool_name: str,
    is_error: bool,
    summarizer_config: DMRConfig | None,
    threshold: int,
) -> str:
    if summarizer_config is not None:
        try:
            result = _route_summarization(
                output,
                config=summarizer_config,
                tool_name=tool_name,
                is_error=is_error,
            )
            logger.info("[Summarizer] AI summary produced (%d chars)", len(result))
            return result
        except Exception:
            logger.warning(
                "[Summarizer] AI summarization failed, falling back to truncation",
                exc_info=True,
            )

    logger.info("[Summarizer] Using truncation fallback (75/25 split)")
    return _truncate_output(output, max_length=threshold)


def _route_summarization(
    output: str,
    *,
    config: DMRConfig,
    tool_name: str,
    is_error: bool,
) -> str:
    chunks = _split_into_chunks(output, _get_chunk_size())
    tool_context = _build_tool_context(tool_name, is_error)

    if len(chunks) == 1:
        return _summarize_single(chunks[0], config=config, tool_context=tool_context)

    logger.info("[Summarizer] Map-reduce: %d chunks", len(chunks))
    chunk_summaries = _map_summarize(chunks, config=config, tool_context=tool_context)
    return _reduce_summaries(chunk_summaries, config=config, tool_context=tool_context)


def _build_tool_context(tool_name: str, is_error: bool) -> str:
    if not tool_name:
        return ""
    status = "FAILED" if is_error else "SUCCESS"
    return f"Tool: {tool_name} | Status: {status}"


def _get_chunk_size() -> int:
    return int(settings.OUTPUT_SUMMARIZE_CHUNK_SIZE)


def _split_into_chunks(text: str, chunk_size: int) -> list[str]:
    chunks: list[str] = []
    for i in range(0, len(text), chunk_size):
        chunks.append(text[i : i + chunk_size])
    return chunks


def _summarize_single(text: str, *, config: DMRConfig, tool_context: str) -> str:
    prompt = _build_chunk_prompt(text, tool_context=tool_context)
    content = _call_summarizer(prompt, config=config)
    return f"[AI Summary] {content}"


def _map_summarize(
    chunks: list[str], *, config: DMRConfig, tool_context: str
) -> list[str]:
    summaries: list[str] = []
    for idx, chunk in enumerate(chunks):
        logger.info("[Summarizer] Summarizing chunk %d/%d", idx + 1, len(chunks))
        prompt = _build_chunk_prompt(
            chunk,
            tool_context=tool_context,
            chunk_label=f"Chunk {idx + 1}/{len(chunks)}",
        )
        summary = _call_summarizer(prompt, config=config)
        summaries.append(summary)
    return summaries


def _reduce_summaries(
    summaries: list[str], *, config: DMRConfig, tool_context: str
) -> str:
    prompt = _build_reduce_prompt(summaries, tool_context=tool_context)
    content = _call_summarizer(prompt, config=config)
    return f"[AI Summary] {content}"


# -- Prompt builders ---------------------------------------------------------


def _build_chunk_prompt(text: str, *, tool_context: str, chunk_label: str = "") -> str:
    prompt = _BASE_SUMMARY_INSTRUCTIONS
    prompt += "Summarize the following command output.\n\n"
    if chunk_label:
        prompt += f"Note: This is {chunk_label} of a larger output.\n\n"
    if tool_context:
        prompt += f"Context: {tool_context}\n\n"
    prompt += f"Output:\n{text}"
    return prompt


def _build_reduce_prompt(summaries: list[str], *, tool_context: str) -> str:
    numbered = "\n".join(f"[Chunk {i + 1}] {s}" for i, s in enumerate(summaries))
    prompt = _BASE_SUMMARY_INSTRUCTIONS
    prompt += (
        "Below are summaries of consecutive chunks of a single command output. "
        "Merge them into ONE final summary.\n\n"
    )
    if tool_context:
        prompt += f"Context: {tool_context}\n\n"
    prompt += f"Chunk summaries:\n{numbered}"
    return prompt


# -- Summarizer transport (routes via dmr_client) ---------------------------


def _call_summarizer(prompt: str, *, config: DMRConfig) -> str:
    messages = (
        ChatMessage(
            role="system",
            content="You summarize command outputs concisely.",
        ),
        ChatMessage(role="user", content=prompt),
    )
    response = send_chat_completion(config, messages)
    content = response.message.content
    if isinstance(content, str):
        return content.strip()
    return ""


# -- Truncation fallback -----------------------------------------------------


def _truncate_output(output: str, *, max_length: int) -> str:
    head_size = int(max_length * OUTPUT_HEAD_RATIO)
    tail_size = max_length - head_size
    return (
        output[:head_size] + "\n\n... [output truncated] ...\n\n" + output[-tail_size:]
    )
