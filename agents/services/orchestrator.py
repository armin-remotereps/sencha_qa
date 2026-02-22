from __future__ import annotations

import json
import logging
import re
from typing import Literal

from django.conf import settings

from agents.services.dmr_client import send_chat_completion
from agents.services.dmr_config import (
    build_orchestrator_config,
    build_sub_agent_config,
    build_vision_config,
)
from agents.services.dmr_model_manager import ensure_model_available, warm_up_model
from agents.services.orchestrator_prompts import (
    build_evaluate_prompt,
    build_evaluate_system_prompt,
    build_plan_system_prompt,
    build_verdict_prompt,
)
from agents.services.sub_agent import run_sub_agent
from agents.types import (
    AgentConfig,
    AgentResult,
    AgentStopReason,
    ChatMessage,
    DMRConfig,
    LogCallback,
    OrchestratorDecision,
    OrchestratorResult,
    ScreenshotCallback,
    SubTask,
    SubTaskResult,
)

logger = logging.getLogger(__name__)

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class OrchestratorPlanError(Exception):
    pass


class OrchestratorParseError(Exception):
    pass


def run_orchestrator(
    task_description: str,
    project_id: int,
    *,
    on_log: LogCallback | None = None,
    on_screenshot: ScreenshotCallback | None = None,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
) -> AgentResult:
    orchestrator_dmr = build_orchestrator_config()
    sub_agent_dmr = build_sub_agent_config()
    vision_dmr = build_vision_config()

    _ensure_models_ready(orchestrator_dmr, sub_agent_dmr, vision_dmr)

    _log(
        on_log,
        f"[Orchestrator] Models: orchestrator={orchestrator_dmr.model}, "
        f"sub_agent={sub_agent_dmr.model}, vision={vision_dmr.model}",
    )
    _log(on_log, "[Orchestrator] Planning: decomposing test case into sub-tasks...")

    sub_tasks = _plan_sub_tasks(
        orchestrator_dmr, task_description, project_prompt=project_prompt, on_log=on_log
    )
    _log(
        on_log,
        f"[Orchestrator] Planning complete: {len(sub_tasks)} sub-tasks created.",
    )

    sub_agent_config = _build_sub_agent_execution_config(
        sub_agent_dmr, vision_dmr, on_log, on_screenshot
    )

    orchestrator_result = _execute_sub_tasks(
        orchestrator_dmr=orchestrator_dmr,
        sub_tasks=sub_tasks,
        project_id=project_id,
        sub_agent_config=sub_agent_config,
        system_info=system_info,
        project_prompt=project_prompt,
        on_log=on_log,
    )

    _log(
        on_log,
        f"[Orchestrator] Verdict: {orchestrator_result.status.upper()} "
        f"({orchestrator_result.total_iterations} total iterations)",
    )

    return _to_agent_result(orchestrator_result)


def _ensure_models_ready(
    orchestrator_dmr: DMRConfig,
    sub_agent_dmr: DMRConfig,
    vision_dmr: DMRConfig,
) -> None:
    ensure_model_available(orchestrator_dmr)
    ensure_model_available(sub_agent_dmr)
    if vision_dmr.api_key is None:
        ensure_model_available(vision_dmr)

    warm_up_model(orchestrator_dmr)
    warm_up_model(sub_agent_dmr)
    if vision_dmr.api_key is None:
        warm_up_model(vision_dmr)


def _build_sub_agent_execution_config(
    sub_agent_dmr: DMRConfig,
    vision_dmr: DMRConfig,
    on_log: LogCallback | None,
    on_screenshot: ScreenshotCallback | None,
) -> AgentConfig:
    return AgentConfig(
        dmr=sub_agent_dmr,
        vision_dmr=vision_dmr,
        max_iterations=settings.SUB_AGENT_MAX_ITERATIONS,
        timeout_seconds=settings.SUB_AGENT_TIMEOUT_SECONDS,
        on_log=on_log,
        on_screenshot=on_screenshot,
    )


def _log(on_log: LogCallback | None, message: str) -> None:
    logger.info(message)
    if on_log is not None:
        on_log(message)


def _plan_sub_tasks(
    orchestrator_dmr: DMRConfig,
    task_description: str,
    *,
    project_prompt: str | None = None,
    on_log: LogCallback | None = None,
) -> tuple[SubTask, ...]:
    system_prompt = build_plan_system_prompt(project_prompt=project_prompt)
    messages = (
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=task_description),
    )

    response = send_chat_completion(orchestrator_dmr, messages)
    raw_text = _extract_text(response.message)

    parsed = _parse_json_response(raw_text)
    raw_sub_tasks = parsed.get("sub_tasks", [])
    if not isinstance(raw_sub_tasks, list) or not raw_sub_tasks:
        raise OrchestratorPlanError(
            f"Orchestrator returned invalid plan: {raw_text[:500]}"
        )

    max_subtasks: int = settings.ORCHESTRATOR_MAX_SUBTASKS
    raw_sub_tasks = raw_sub_tasks[:max_subtasks]

    sub_tasks: list[SubTask] = []
    for item in raw_sub_tasks:
        if isinstance(item, dict):
            sub_tasks.append(
                SubTask(
                    description=str(item.get("description", "")),
                    expected_result=str(item.get("expected_result", "")),
                )
            )

    for i, st in enumerate(sub_tasks, 1):
        _log(on_log, f"[Orchestrator]   Sub-task {i}: {st.description}")

    return tuple(sub_tasks)


def _execute_sub_tasks(
    *,
    orchestrator_dmr: DMRConfig,
    sub_tasks: tuple[SubTask, ...],
    project_id: int,
    sub_agent_config: AgentConfig,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
    on_log: LogCallback | None = None,
) -> OrchestratorResult:
    results: list[SubTaskResult] = []
    state_lines: list[str] = []
    total_iterations = 0
    recovery_counts: dict[int, int] = {}
    max_recovery: int = settings.ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS

    evaluate_messages: list[ChatMessage] = [
        ChatMessage(role="system", content=build_evaluate_system_prompt()),
    ]

    total = len(sub_tasks)
    i = 0

    while i < len(sub_tasks):
        sub_task = sub_tasks[i]
        step_num = i + 1
        state_description = "\n".join(state_lines) if state_lines else ""

        _log(
            on_log,
            f"[Orchestrator] Sub-task {step_num}/{total}: {sub_task.description}",
        )

        result = run_sub_agent(
            sub_task,
            state_description,
            project_id,
            config=sub_agent_config,
            system_info=system_info,
            project_prompt=project_prompt,
        )
        total_iterations += result.iterations
        results.append(result)

        _log(
            on_log,
            f"[Orchestrator]   Result: {result.status.upper()} — {result.summary}",
        )

        if result.status == "pass":
            state_lines.append(f"Step {step_num}: {result.summary}")
            i += 1
            continue

        remaining = total - step_num
        decision = _evaluate_failure(
            orchestrator_dmr=orchestrator_dmr,
            evaluate_messages=evaluate_messages,
            sub_task=sub_task,
            sub_task_result=result,
            state_description=state_description,
            remaining_tasks=remaining,
            on_log=on_log,
        )

        if decision.action == "continue":
            state_lines.append(f"Step {step_num}: FAILED — {result.summary}")
            i += 1
            continue

        recovered = _attempt_recovery(
            decision=decision,
            step_index=i,
            step_num=step_num,
            state_description=state_description,
            project_id=project_id,
            sub_agent_config=sub_agent_config,
            system_info=system_info,
            project_prompt=project_prompt,
            results=results,
            state_lines=state_lines,
            recovery_counts=recovery_counts,
            max_recovery=max_recovery,
            on_log=on_log,
        )

        if recovered is not None:
            total_iterations += recovered
            continue

        _log(on_log, f"[Orchestrator]   Stopping: {decision.reason}")
        state_lines.append(f"Step {step_num}: FAILED (stopped) — {result.summary}")
        break

    return _build_verdict(orchestrator_dmr, results, total_iterations)


def _attempt_recovery(
    *,
    decision: OrchestratorDecision,
    step_index: int,
    step_num: int,
    state_description: str,
    project_id: int,
    sub_agent_config: AgentConfig,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    results: list[SubTaskResult],
    state_lines: list[str],
    recovery_counts: dict[int, int],
    max_recovery: int,
    on_log: LogCallback | None,
) -> int | None:
    if decision.action != "recover":
        return None

    attempts = recovery_counts.get(step_index, 0)
    if attempts >= max_recovery:
        _log(on_log, "[Orchestrator]   Max recovery attempts reached — stopping test.")
        return None

    if decision.recovery_task is None:
        _log(
            on_log,
            "[Orchestrator]   Recovery requested but no task provided — stopping test.",
        )
        return None

    recovery_counts[step_index] = attempts + 1

    _log(on_log, f"[Orchestrator]   Recovery: {decision.recovery_task.description}")
    recovery_result = run_sub_agent(
        decision.recovery_task,
        state_description,
        project_id,
        config=sub_agent_config,
        system_info=system_info,
        project_prompt=project_prompt,
    )
    results.append(recovery_result)

    _log(
        on_log,
        f"[Orchestrator]   Recovery result: {recovery_result.status.upper()} "
        f"— {recovery_result.summary}",
    )

    if recovery_result.status == "pass":
        state_lines.append(f"Recovery for step {step_num}: {recovery_result.summary}")
        return recovery_result.iterations

    _log(on_log, "[Orchestrator]   Recovery failed — stopping test.")
    return None


def _build_verdict(
    orchestrator_dmr: DMRConfig,
    results: list[SubTaskResult],
    total_iterations: int,
) -> OrchestratorResult:
    all_passed = all(r.status == "pass" for r in results)
    overall_status: Literal["pass", "fail"] = "pass" if all_passed else "fail"

    verdict_prompt = build_verdict_prompt(tuple(results))
    verdict_messages = (
        ChatMessage(role="system", content="Summarize the test case execution."),
        ChatMessage(role="user", content=verdict_prompt),
    )
    verdict_response = send_chat_completion(orchestrator_dmr, verdict_messages)
    summary = _extract_text(verdict_response.message)

    return OrchestratorResult(
        status=overall_status,
        summary=summary,
        sub_task_results=tuple(results),
        total_iterations=total_iterations,
    )


def _evaluate_failure(
    *,
    orchestrator_dmr: DMRConfig,
    evaluate_messages: list[ChatMessage],
    sub_task: SubTask,
    sub_task_result: SubTaskResult,
    state_description: str,
    remaining_tasks: int,
    on_log: LogCallback | None = None,
) -> OrchestratorDecision:
    prompt = build_evaluate_prompt(
        sub_task, sub_task_result, state_description, remaining_tasks
    )
    evaluate_messages.append(ChatMessage(role="user", content=prompt))

    response = send_chat_completion(orchestrator_dmr, tuple(evaluate_messages))
    raw_text = _extract_text(response.message)
    evaluate_messages.append(ChatMessage(role="assistant", content=raw_text))

    parsed = _parse_json_response(raw_text)
    raw_action = str(parsed.get("decision", "stop"))
    reason = str(parsed.get("reason", ""))

    action: Literal["continue", "recover", "stop"]
    if raw_action == "continue":
        action = "continue"
    elif raw_action == "recover":
        action = "recover"
    else:
        action = "stop"

    _log(on_log, f"[Orchestrator]   Decision: {action} — {reason}")

    recovery_task: SubTask | None = None
    if action == "recover":
        raw_recovery = parsed.get("recovery_task")
        if isinstance(raw_recovery, dict):
            recovery_task = SubTask(
                description=str(raw_recovery.get("description", "")),
                expected_result=str(raw_recovery.get("expected_result", "")),
            )

    return OrchestratorDecision(
        action=action,
        reason=reason,
        recovery_task=recovery_task,
    )


def _to_agent_result(orchestrator_result: OrchestratorResult) -> AgentResult:
    if orchestrator_result.status == "pass":
        stop_reason = AgentStopReason.TASK_COMPLETE
    else:
        stop_reason = AgentStopReason.ERROR

    messages = (ChatMessage(role="assistant", content=orchestrator_result.summary),)

    error: str | None = None
    if orchestrator_result.status == "fail":
        failed = [r for r in orchestrator_result.sub_task_results if r.status == "fail"]
        if failed:
            error = failed[-1].summary

    return AgentResult(
        stop_reason=stop_reason,
        iterations=orchestrator_result.total_iterations,
        messages=messages,
        error=error,
    )


def _extract_text(message: ChatMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return ""


def _parse_json_response(text: str) -> dict[str, object]:
    block_match = _JSON_BLOCK_PATTERN.search(text)
    json_text = block_match.group(1) if block_match else text

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        try:
            clean = _extract_json_object(json_text)
            parsed = json.loads(clean)
        except (ValueError, json.JSONDecodeError) as exc:
            raise OrchestratorParseError(
                f"Failed to parse JSON from LLM response: {text[:300]}"
            ) from exc

    if not isinstance(parsed, dict):
        raise OrchestratorParseError(f"Expected JSON object, got: {type(parsed)}")
    return parsed


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in text: {text[:200]}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]
