from __future__ import annotations

import logging

import httpx
from django.conf import settings

from agents.types import DMRConfig

logger = logging.getLogger(__name__)

OUTPUT_HEAD_RATIO = 0.75


def summarize_output(
    output: str,
    *,
    tool_name: str = "",
    is_error: bool = False,
    summarizer_config: DMRConfig | None = None,
) -> str:
    """Summarize *output* if it exceeds the configured threshold.

    When a DMR summarizer config is provided the output is sent to the AI
    model for an intelligent summary.  If the DMR call fails (or no config
    is given) the function falls back to a 75/25 head/tail truncation.
    """
    threshold: int = int(settings.OUTPUT_SUMMARIZE_THRESHOLD)
    if len(output) <= threshold:
        return output

    logger.info(
        "[Summarizer] Output exceeds threshold (%d > %d chars), summarizing...",
        len(output),
        threshold,
    )

    if summarizer_config is not None:
        try:
            result = _ai_summarize(
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


def _ai_summarize(
    output: str,
    *,
    config: DMRConfig,
    tool_name: str,
    is_error: bool,
) -> str:
    """Summarize *output* using map-reduce when it exceeds chunk size."""
    chunk_size: int = int(settings.OUTPUT_SUMMARIZE_CHUNK_SIZE)
    status_hint = "FAILED" if is_error else "SUCCESS"
    context_line = f"Tool: {tool_name} | Status: {status_hint}" if tool_name else ""

    chunks = _split_into_chunks(output, chunk_size)

    if len(chunks) == 1:
        return _summarize_single(chunks[0], config=config, context_line=context_line)

    logger.info("[Summarizer] Map-reduce: %d chunks", len(chunks))
    chunk_summaries = _map_summarize(chunks, config=config, context_line=context_line)
    return _reduce_summaries(chunk_summaries, config=config, context_line=context_line)


def _split_into_chunks(text: str, chunk_size: int) -> list[str]:
    """Split *text* into chunks of at most *chunk_size* characters."""
    chunks: list[str] = []
    for i in range(0, len(text), chunk_size):
        chunks.append(text[i : i + chunk_size])
    return chunks


def _summarize_single(text: str, *, config: DMRConfig, context_line: str) -> str:
    """Summarize a single chunk directly."""
    prompt = _build_chunk_prompt(text, context_line=context_line)
    timeout = float(settings.DMR_REQUEST_TIMEOUT)
    with httpx.Client(timeout=timeout) as client:
        content = _call_dmr(prompt, config=config, client=client)
    return f"[AI Summary] {content}"


def _map_summarize(
    chunks: list[str], *, config: DMRConfig, context_line: str
) -> list[str]:
    """Map step: summarize each chunk independently."""
    summaries: list[str] = []
    timeout = float(settings.DMR_REQUEST_TIMEOUT)
    with httpx.Client(timeout=timeout) as client:
        for idx, chunk in enumerate(chunks):
            logger.info("[Summarizer] Summarizing chunk %d/%d", idx + 1, len(chunks))
            prompt = _build_chunk_prompt(
                chunk,
                context_line=context_line,
                chunk_label=f"Chunk {idx + 1}/{len(chunks)}",
            )
            summary = _call_dmr(prompt, config=config, client=client)
            summaries.append(summary)
    return summaries


def _reduce_summaries(
    summaries: list[str], *, config: DMRConfig, context_line: str
) -> str:
    """Reduce step: merge chunk summaries into a final summary."""
    numbered = "\n".join(f"[Chunk {i + 1}] {s}" for i, s in enumerate(summaries))
    prompt = (
        "You are a concise output summarizer for an automated test agent. "
        "Below are summaries of consecutive chunks of a single command output. "
        "Merge them into ONE final summary. Your summary MUST include:\n"
        "1. status: SUCCESS or FAILED\n"
        "2. reason: one-line explanation of what happened\n"
        "3. key_info: preserve any critical error messages, file paths, "
        "version numbers, or actionable details\n\n"
        "Keep the final summary under 800 characters.\n\n"
    )
    if context_line:
        prompt += f"Context: {context_line}\n\n"
    prompt += f"Chunk summaries:\n{numbered}"

    timeout = float(settings.DMR_REQUEST_TIMEOUT)
    with httpx.Client(timeout=timeout) as client:
        content = _call_dmr(prompt, config=config, client=client)
    return f"[AI Summary] {content}"


def _build_chunk_prompt(text: str, *, context_line: str, chunk_label: str = "") -> str:
    """Build the prompt for summarizing a single chunk."""
    prompt = (
        "You are a concise output summarizer for an automated test agent. "
        "Summarize the following command output. Your summary MUST include:\n"
        "1. status: SUCCESS or FAILED\n"
        "2. reason: one-line explanation of what happened\n"
        "3. key_info: preserve any critical error messages, file paths, "
        "version numbers, or actionable details\n\n"
        "Keep the summary under 800 characters.\n\n"
    )
    if chunk_label:
        prompt += f"Note: This is {chunk_label} of a larger output.\n\n"
    if context_line:
        prompt += f"Context: {context_line}\n\n"
    prompt += f"Output:\n{text}"
    return prompt


def _call_dmr(prompt: str, *, config: DMRConfig, client: httpx.Client) -> str:
    """Send a prompt to the DMR summarizer and return the content string."""
    url = f"http://{config.host}:{config.port}/engines/llama.cpp/v1/chat/completions"
    payload: dict[str, object] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": "You summarize command outputs concisely."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }

    response = client.post(url, json=payload)
    response.raise_for_status()

    data: object = response.json()
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


def _truncate_output(output: str, *, max_length: int) -> str:
    """Truncate with a 75/25 head/tail split."""
    head_size = int(max_length * OUTPUT_HEAD_RATIO)
    tail_size = max_length - head_size
    return (
        output[:head_size] + "\n\n... [output truncated] ...\n\n" + output[-tail_size:]
    )
