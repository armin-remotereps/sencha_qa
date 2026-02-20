from __future__ import annotations

import logging
import re
from typing import Literal

from agents.services.agent_loop import _run_agent_loop
from agents.services.dmr_config import build_summarizer_config
from agents.services.sub_agent_prompts import build_sub_agent_system_prompt
from agents.types import (
    AgentConfig,
    AgentResult,
    AgentStopReason,
    SubTask,
    SubTaskResult,
    ToolContext,
)

logger = logging.getLogger(__name__)

_RESULT_PATTERN = re.compile(r"RESULT:\s*(PASS|FAIL)", re.IGNORECASE)
_SUMMARY_PATTERN = re.compile(r"SUMMARY:\s*(.+)", re.IGNORECASE | re.DOTALL)

SubTaskStatus = Literal["pass", "fail"]


def run_sub_agent(
    sub_task: SubTask,
    state_description: str,
    project_id: int,
    *,
    config: AgentConfig,
    system_info: dict[str, object] | None = None,
) -> SubTaskResult:
    system_prompt = build_sub_agent_system_prompt(
        sub_task.description,
        sub_task.expected_result,
        state_description,
        system_info=system_info,
    )

    summarizer_config = build_summarizer_config()

    context = ToolContext(
        project_id=project_id,
        summarizer_config=summarizer_config,
        vision_config=config.vision_dmr,
        on_screenshot=config.on_screenshot,
        on_log=config.on_log,
    )

    task_description = (
        f"Execute this step: {sub_task.description}\n"
        f"Expected result: {sub_task.expected_result}"
    )

    agent_result = _run_agent_loop(
        task_description,
        context,
        config=config,
        system_prompt=system_prompt,
    )

    return _parse_sub_task_result(agent_result)


def _parse_sub_task_result(agent_result: AgentResult) -> SubTaskResult:
    last_text = _extract_last_assistant_text(agent_result)

    if last_text:
        status, summary = _parse_result_text(last_text)
        if status:
            return SubTaskResult(
                status=status,
                summary=summary,
                iterations=agent_result.iterations,
                error=agent_result.error,
            )

    if agent_result.stop_reason == AgentStopReason.TASK_COMPLETE:
        return SubTaskResult(
            status="pass",
            summary=last_text or "Step completed successfully.",
            iterations=agent_result.iterations,
        )

    return SubTaskResult(
        status="fail",
        summary=last_text or agent_result.error or "Step failed.",
        iterations=agent_result.iterations,
        error=agent_result.error,
    )


def _extract_last_assistant_text(agent_result: AgentResult) -> str:
    for message in reversed(agent_result.messages):
        if message.role == "assistant" and isinstance(message.content, str):
            return message.content
    return ""


def _parse_result_text(text: str) -> tuple[SubTaskStatus | None, str]:
    result_match = _RESULT_PATTERN.search(text)
    if not result_match:
        return None, text

    status: SubTaskStatus = (
        "pass" if result_match.group(1).upper() == "PASS" else "fail"
    )

    summary_match = _SUMMARY_PATTERN.search(text)
    summary = summary_match.group(1).strip() if summary_match else text

    return status, summary
