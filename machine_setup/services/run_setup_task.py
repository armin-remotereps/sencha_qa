"""Run one sub-task the main setup agent delegated with ``run_setup_task``.

The sub-agent starts from a fresh ``[system, user]`` context, drives the test
machine with the tester's tools, and hands back only its parsed report. Its
message list lives in memory for the call and is never persisted; its tool
rows are recorded nested under the delegation row.

Exceptions (Stop, a lost runner, provider faults, the Celery soft limit)
propagate unchanged and end the turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from agents.services.agent_loop import run_tool_loop
from agents.types import (
    AgentConfig,
    CancellationCheck,
    ChatMessage,
    LLMConfig,
    ToolCall,
    ToolContext,
    ToolResult,
)
from machine_setup.exceptions import SetupRunnerLostError
from machine_setup.models import SetupMessage
from machine_setup.services.activity_recorder import ActivityRecorder
from machine_setup.services.setup_task_prompt import build_setup_task_system_prompt
from machine_setup.services.setup_tools import (
    build_machine_dispatcher,
    machine_tool_definitions,
)
from machine_setup.subtask_budget import build_subtask_bounds, resolve_subtask_budget
from machine_setup.subtask_report import parse_subtask_report, render_report_for_main
from machine_setup.subtask_types import SetupTaskRequest
from projects.controller_outage_clock import agent_monotonic
from projects.controller_reconnect_services import connected_owner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SetupTaskRuntime:
    """What every sub-task of one turn shares; built once by the turn runner."""

    project_id: int
    conversation_id: int
    turn_id: int
    system_info: dict[str, object] | None
    # The application context as it was at turn start.
    project_prompt: str | None
    llm_config: LLMConfig
    cancellation_check: CancellationCheck


def run_setup_task(
    tool_call: ToolCall,
    request: SetupTaskRequest,
    context: ToolContext,
    *,
    runtime: SetupTaskRuntime,
    parent_row: SetupMessage | None,
) -> ToolResult:
    if parent_row is None:
        # The main recorder always has the delegation row while dispatching.
        logger.error(
            "Setup turn %s: run_setup_task %s has no display row",
            runtime.turn_id,
            tool_call.tool_call_id,
        )
        return _error(tool_call, "Not run: internal error, try again.")

    now = agent_monotonic()
    budget = resolve_subtask_budget(
        request.requested_timeout,
        bounds=build_subtask_bounds(),
        deadline=context.deadline,
        now=now,
    )
    if budget is None:
        left = max(int((context.deadline or now) - now), 0)
        return _error(
            tool_call,
            f"Only {left} s are left in this turn, too little for a sub-task. "
            "Tell the user what is done and that they can reply 'continue'.",
        )

    # Checked before the first LLM call so a lost runner costs no model round.
    if connected_owner(runtime.project_id) is None:
        raise SetupRunnerLostError("The Test Runner is not connected.")

    max_iterations = settings.MACHINE_SETUP_SUBTASK_MAX_ITERATIONS
    system_prompt = build_setup_task_system_prompt(
        system_info=runtime.system_info,
        project_prompt=runtime.project_prompt,
        current_browser=context.browser_selection.current,
        timeout_seconds=budget,
        max_iterations=max_iterations,
        read_only=request.read_only,
    )
    recorder = ActivityRecorder(
        project_id=runtime.project_id,
        conversation_id=runtime.conversation_id,
        turn_id=runtime.turn_id,
        parent=parent_row,
        record_final_reply=False,
    )
    config = AgentConfig(
        llm=runtime.llm_config,
        vision_llm=runtime.llm_config,
        max_iterations=max_iterations,
        timeout_seconds=budget,
        on_screenshot=recorder.on_screenshot,
        cancellation_check=runtime.cancellation_check,
    )
    sub_context = ToolContext(
        project_id=runtime.project_id,
        # Shared with the main loop so a browser picked here stays picked.
        browser_selection=context.browser_selection,
        summarizer_config=runtime.llm_config,
        vision_config=runtime.llm_config,
        on_screenshot=recorder.on_screenshot,
        on_log=_log_tool_debug,
        cancellation_check=runtime.cancellation_check,
    )
    logger.info(
        "Setup turn %s: sub-task %s started (budget %s s, read_only=%s)",
        runtime.turn_id,
        parent_row.pk,
        budget,
        request.read_only,
    )
    started = agent_monotonic()
    result = run_tool_loop(
        [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=_task_message(request)),
        ],
        sub_context,
        config=config,
        tool_definitions=machine_tool_definitions(),
        dispatch=recorder.wrap(build_machine_dispatcher(runtime.project_id)),
        on_transcript=lambda message, _transcript: recorder.on_message(message),
    )
    elapsed = int(agent_monotonic() - started)

    report = parse_subtask_report(result)
    # Same instance the main recorder holds: its result update broadcasts this.
    parent_row.subtask_result = report.result
    parent_row.save(update_fields=["subtask_result", "updated_at"])
    logger.info(
        "Setup turn %s: sub-task %s ended %s after %s rounds, %s s",
        runtime.turn_id,
        parent_row.pk,
        report.result,
        result.iterations,
        elapsed,
    )
    return ToolResult(
        tool_call_id=tool_call.tool_call_id,
        content=render_report_for_main(
            report,
            steps=result.iterations,
            elapsed_seconds=elapsed,
            budget_seconds=budget,
        ),
        is_error=False,
    )


def _task_message(request: SetupTaskRequest) -> str:
    known_state = request.known_state or "Nothing beyond the project context."
    mode = (
        "\n\nMODE: read-only. Inspect only; change nothing."
        if request.read_only
        else ""
    )
    return (
        f"GOAL:\n{request.goal}\n\n"
        f"DONE WHEN:\n{request.done_when}\n\n"
        f"KNOWN STATE (reference facts from earlier work and the user):\n"
        f"{known_state}"
        f"{mode}"
    )


def _error(tool_call: ToolCall, text: str) -> ToolResult:
    return ToolResult(tool_call_id=tool_call.tool_call_id, content=text, is_error=True)


def _log_tool_debug(message: str) -> None:
    # Tool output can hold secrets the user typed; never above DEBUG.
    logger.debug(message)
