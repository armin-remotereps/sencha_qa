from __future__ import annotations

import logging
import re
from typing import Literal

from agents.attempt_digest import build_attempt_digest
from agents.services.agent_loop import _run_agent_loop
from agents.services.llm_config import build_llm_config
from agents.services.sub_agent_prompts import build_sub_agent_system_prompt
from agents.services.tool_registry import (
    allowlisted_dispatcher,
    tool_definitions_named,
)
from agents.types import (
    AgentConfig,
    AgentResult,
    AgentStopReason,
    BrowserSelection,
    SubTask,
    SubTaskResult,
    ToolContext,
)

logger = logging.getLogger(__name__)

_RESULT_PATTERN = re.compile(r"RESULT:\s*(PASS|FAIL)", re.IGNORECASE)
_SUMMARY_PATTERN = re.compile(r"SUMMARY:\s*(.+)", re.IGNORECASE | re.DOTALL)

SubTaskStatus = Literal["pass", "fail"]

# Marks a PASS given in the tool-free turn after the time budget ran out, so the
# verdict and a person reviewing the case can see it.
_ANSWERED_AFTER_TIMEOUT_SUFFIX = " (answered when the step's time budget ran out)"


def run_sub_agent(
    sub_task: SubTask,
    state_description: str,
    project_id: int,
    *,
    config: AgentConfig,
    browser_selection: BrowserSelection,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
    tool_names: frozenset[str] | None = None,
) -> SubTaskResult:
    """Run one sub-task; ``tool_names`` limits the tools it may see and call."""
    system_prompt = build_sub_agent_system_prompt(
        sub_task.description,
        sub_task.expected_result,
        state_description,
        timeout_seconds=config.timeout_seconds,
        current_browser=browser_selection.current,
        system_info=system_info,
        project_prompt=project_prompt,
        tool_names=tool_names,
        final_turn_on_timeout=config.final_answer is not None,
    )

    summarizer_config = build_llm_config()

    context = ToolContext(
        project_id=project_id,
        browser_selection=browser_selection,
        summarizer_config=summarizer_config,
        vision_config=config.vision_llm,
        on_screenshot=config.on_screenshot,
        on_log=config.on_log,
        cancellation_check=config.cancellation_check,
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
        tool_definitions=(
            None if tool_names is None else tool_definitions_named(tool_names)
        ),
        dispatch=None if tool_names is None else allowlisted_dispatcher(tool_names),
    )

    return _parse_sub_task_result(agent_result)


def _parse_sub_task_result(agent_result: AgentResult) -> SubTaskResult:
    last_text = _extract_last_assistant_text(agent_result)

    if last_text:
        status, summary = _parse_result_text(last_text)
        if status:
            logger.info(
                "Sub-agent result parsing: found explicit RESULT: %s", status.upper()
            )
            if status == "pass" and agent_result.answered_after_timeout:
                # Counts as a normal pass: the final-answer prompt allowed PASS
                # only on evidence already seen in the conversation.
                return SubTaskResult(
                    status="pass",
                    summary=f"{summary}{_ANSWERED_AFTER_TIMEOUT_SUFFIX}",
                    iterations=agent_result.iterations,
                )
            return SubTaskResult(
                status=status,
                summary=summary,
                iterations=agent_result.iterations,
                error=agent_result.error,
                actions_digest=(
                    build_attempt_digest(agent_result.messages)
                    if status == "fail"
                    else ""
                ),
            )

    if agent_result.stop_reason == AgentStopReason.TASK_COMPLETE:
        logger.info(
            "Sub-agent result parsing: no explicit RESULT, "
            "stop_reason=TASK_COMPLETE — auto-passing"
        )
        return SubTaskResult(
            status="pass",
            summary=last_text or "Step completed successfully.",
            iterations=agent_result.iterations,
        )

    logger.info(
        "Sub-agent result parsing: no explicit RESULT, "
        "stop_reason=%s — defaulting to fail",
        agent_result.stop_reason,
    )
    return SubTaskResult(
        status="fail",
        summary=last_text or agent_result.error or "Step failed.",
        iterations=agent_result.iterations,
        error=agent_result.error,
        actions_digest=build_attempt_digest(agent_result.messages),
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
