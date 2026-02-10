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
    """Call the DMR summarizer model to summarize *output*."""
    status_hint = "FAILED" if is_error else "SUCCESS"
    context_line = f"Tool: {tool_name} | Status: {status_hint}" if tool_name else ""

    prompt = (
        "You are a concise output summarizer for an automated test agent. "
        "Summarize the following command output. Your summary MUST include:\n"
        "1. status: SUCCESS or FAILED\n"
        "2. reason: one-line explanation of what happened\n"
        "3. key_info: preserve any critical error messages, file paths, "
        "version numbers, or actionable details\n\n"
        "Keep the summary under 800 characters.\n\n"
    )
    if context_line:
        prompt += f"Context: {context_line}\n\n"
    prompt += f"Output:\n{output}"

    url = f"http://{config.host}:{config.port}/engines/llama.cpp/v1/chat/completions"
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": "You summarize command outputs concisely."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()

    data = response.json()
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

    return f"[AI Summary] {content.strip()}"


def _truncate_output(output: str, *, max_length: int) -> str:
    """Truncate with a 75/25 head/tail split."""
    head_size = int(max_length * OUTPUT_HEAD_RATIO)
    tail_size = max_length - head_size
    return (
        output[:head_size] + "\n\n... [output truncated] ...\n\n" + output[-tail_size:]
    )
