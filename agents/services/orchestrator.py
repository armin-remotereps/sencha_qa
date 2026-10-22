from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Literal

from django.conf import settings

from agents.exceptions import (
    AIServiceError,
    OrchestratorParseError,
    OrchestratorPlanError,
    ResumeStateLostError,
)
from agents.services.llm_client import send_chat_completion
from agents.services.llm_config import (
    build_llm_config,
)
from agents.services.orchestrator_prompts import (
    build_evaluate_prompt,
    build_evaluate_system_prompt,
    build_plan_system_prompt,
    build_verdict_prompt,
    final_step_indices,
    format_attempt_lines,
    was_resumed,
)
from agents.services.sub_agent import run_sub_agent
from agents.services.sub_task_budget import (
    TimeoutBounds,
    build_timeout_bounds,
    resolve_timeout_seconds,
)
from agents.types import (
    AgentCancelledError,
    AgentConfig,
    AgentResult,
    AgentStopReason,
    BrowserSelection,
    CancellationCheck,
    ChatMessage,
    CheckpointCallback,
    LLMConfig,
    LogCallback,
    OrchestratorCheckpoint,
    OrchestratorDecision,
    OrchestratorResult,
    ScreenshotCallback,
    SubTask,
    SubTaskAttempt,
    SubTaskResult,
)

logger = logging.getLogger(__name__)

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def run_orchestrator(
    task_description: str,
    project_id: int,
    *,
    on_log: LogCallback | None = None,
    on_screenshot: ScreenshotCallback | None = None,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
    cancellation_check: CancellationCheck | None = None,
    resume: OrchestratorCheckpoint | None = None,
    on_checkpoint: CheckpointCallback | None = None,
) -> AgentResult:
    # One model serves every role. The distinct names keep the call sites below
    # readable and make a future per-role split a one-line change.
    orchestrator_llm = build_llm_config()
    sub_agent_llm = orchestrator_llm
    vision_llm = orchestrator_llm

    _log(on_log, f"[Orchestrator] Model: {orchestrator_llm.model}")

    bounds = build_timeout_bounds()
    if resume is None:
        _log(on_log, "[Orchestrator] Planning: decomposing test case into sub-tasks...")
        sub_tasks = _plan_sub_tasks(
            orchestrator_llm,
            task_description,
            bounds=bounds,
            project_prompt=project_prompt,
            on_log=on_log,
        )
    else:
        sub_tasks = resume.plan
        _log(
            on_log,
            f"[Orchestrator] Resuming at step {resume.next_step} of "
            f"{len(sub_tasks)} from the saved plan.",
        )

    _check_cancelled(cancellation_check, on_log)

    if resume is None:
        _log(
            on_log,
            f"[Orchestrator] Planning complete: {len(sub_tasks)} sub-tasks created.",
        )

    sub_agent_config = _build_sub_agent_execution_config(
        sub_agent_llm, vision_llm, on_log, on_screenshot, cancellation_check
    )
    # One selection per case (one run_orchestrator call per case), shared by
    # every sub-agent and recovery sub-agent, so the browser the case asked for
    # on its first navigate stays selected and the next case starts on Chromium.
    # A resumed case keeps the browser it had already selected.
    browser_selection = (
        BrowserSelection()
        if resume is None
        else BrowserSelection(current=resume.browser)
    )
    progress = _new_progress(sub_tasks, resume, browser_selection, on_checkpoint)
    progress.checkpoint()

    orchestrator_result = _execute_sub_tasks(
        orchestrator_llm=orchestrator_llm,
        progress=progress,
        project_id=project_id,
        sub_agent_config=sub_agent_config,
        bounds=bounds,
        resuming=resume is not None,
        system_info=system_info,
        project_prompt=project_prompt,
        on_log=on_log,
        cancellation_check=cancellation_check,
    )

    _log(
        on_log,
        f"[Orchestrator] Verdict: {orchestrator_result.status.upper()} "
        f"({orchestrator_result.total_iterations} total iterations)",
    )

    return _to_agent_result(orchestrator_result)


@dataclass
class _Progress:
    """Mutable progress of one case; mirrors what a checkpoint persists."""

    plan: tuple[SubTask, ...]
    browser_selection: BrowserSelection
    on_checkpoint: CheckpointCallback | None
    attempts: list[SubTaskAttempt] = field(default_factory=list)
    state_lines: list[str] = field(default_factory=list)
    total_iterations: int = 0
    # 1-based step that is running or runs next.
    next_step: int = 1

    def checkpoint(self) -> None:
        if self.on_checkpoint is None:
            return
        self.on_checkpoint(
            OrchestratorCheckpoint(
                plan=self.plan,
                attempts=tuple(self.attempts),
                next_step=self.next_step,
                browser=self.browser_selection.current,
                total_iterations=self.total_iterations,
            )
        )


def _new_progress(
    plan: tuple[SubTask, ...],
    resume: OrchestratorCheckpoint | None,
    browser_selection: BrowserSelection,
    on_checkpoint: CheckpointCallback | None,
) -> _Progress:
    progress = _Progress(
        plan=plan,
        browser_selection=browser_selection,
        on_checkpoint=on_checkpoint,
    )
    if resume is None:
        return progress
    progress.attempts = list(resume.attempts)
    progress.total_iterations = resume.total_iterations
    progress.next_step = resume.next_step
    progress.state_lines = _rebuild_state_lines(resume.attempts, resume.next_step)
    return progress


def _rebuild_state_lines(
    attempts: tuple[SubTaskAttempt, ...], next_step: int
) -> list[str]:
    """Rebuild the state lines ``_execute_sub_tasks`` wrote for these attempts.

    A failed step only got a line when the run moved past it ("continue"); one
    followed by a recovery, or still at the cursor, got none.
    """
    final_index = final_step_indices(attempts)
    lines: list[str] = []
    for index, attempt in enumerate(attempts):
        result = attempt.result
        if attempt.kind == "step":
            if result.status == "pass":
                lines.append(f"Step {attempt.step_num}: {result.summary}")
            elif (
                final_index[attempt.step_num] == index and attempt.step_num < next_step
            ):
                lines.append(f"Step {attempt.step_num}: FAILED — {result.summary}")
        elif result.status != "pass":
            continue
        elif attempt.kind == "recovery":
            lines.append(f"Recovery for step {attempt.step_num}: {result.summary}")
        else:
            lines.append(_resume_state_line(attempt.step_num, result.summary))
    return lines


def _resume_state_line(step_num: int, summary: str) -> str:
    return f"Resumed: state restored before step {step_num} — {summary}"


def _build_resume_sub_task(
    plan: tuple[SubTask, ...], next_step: int, bounds: TimeoutBounds
) -> SubTask:
    done_lines = "\n".join(
        f"{num}. {st.description} (expected: {st.expected_result})"
        for num, st in enumerate(plan[: next_step - 1], 1)
    )
    description = (
        "These steps of the test case already ran:\n"
        f"{done_lines}\n\n"
        "The test was interrupted and the test machine may have been restarted or "
        "changed by a person. Make sure the machine is in the state those steps "
        "left it in (applications open, pages loaded, files present, logged in), "
        "redoing whatever is no longer in effect. Do not repeat actions whose "
        "effect is still in place. Do not judge the application; only restore "
        "state."
    )
    expected_result = (
        "The machine is in the state described above, ready for step "
        f"{next_step}: {plan[next_step - 1].description}"
    )
    return SubTask(
        description=description,
        expected_result=expected_result,
        timeout_seconds=bounds.maximum,
    )


def _restore_resumed_state(
    *,
    progress: _Progress,
    project_id: int,
    sub_agent_config: AgentConfig,
    bounds: TimeoutBounds,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    on_log: LogCallback | None,
) -> None:
    step_num = progress.next_step
    resume_task = _build_resume_sub_task(progress.plan, step_num, bounds)
    _log(
        on_log,
        f"[Orchestrator] Restoring state before step {step_num} "
        f"(budget {resume_task.timeout_seconds}s)",
    )
    result = run_sub_agent(
        resume_task,
        "\n".join(progress.state_lines),
        project_id,
        config=_config_for_sub_task(sub_agent_config, resume_task),
        browser_selection=progress.browser_selection,
        system_info=system_info,
        project_prompt=project_prompt,
    )
    progress.total_iterations += result.iterations
    progress.attempts.append(
        SubTaskAttempt(step_num=step_num, kind="resume", result=result)
    )
    progress.checkpoint()

    _log(
        on_log,
        f"[Orchestrator]   Restore result: {result.status.upper()} — {result.summary}",
    )

    if result.status != "pass":
        raise ResumeStateLostError(step_num, result.summary)
    progress.state_lines.append(_resume_state_line(step_num, result.summary))


def _build_sub_agent_execution_config(
    sub_agent_llm: LLMConfig,
    vision_llm: LLMConfig,
    on_log: LogCallback | None,
    on_screenshot: ScreenshotCallback | None,
    cancellation_check: CancellationCheck | None,
) -> AgentConfig:
    return AgentConfig(
        llm=sub_agent_llm,
        vision_llm=vision_llm,
        max_iterations=settings.SUB_AGENT_MAX_ITERATIONS,
        timeout_seconds=settings.SUB_AGENT_TIMEOUT_SECONDS,
        on_log=on_log,
        on_screenshot=on_screenshot,
        cancellation_check=cancellation_check,
    )


def _log(on_log: LogCallback | None, message: str) -> None:
    logger.info(message)
    if on_log is not None:
        on_log(message)


def _check_cancelled(
    cancellation_check: CancellationCheck | None,
    on_log: LogCallback | None,
) -> None:
    if cancellation_check is not None and cancellation_check():
        _log(on_log, "[Orchestrator] Cancellation detected")
        raise AgentCancelledError("Orchestrator cancelled by external request")


def _plan_sub_tasks(
    orchestrator_llm: LLMConfig,
    task_description: str,
    *,
    bounds: TimeoutBounds,
    project_prompt: str | None = None,
    on_log: LogCallback | None = None,
) -> tuple[SubTask, ...]:
    system_prompt = build_plan_system_prompt(
        project_prompt=project_prompt,
        min_timeout_seconds=bounds.minimum,
        max_timeout_seconds=bounds.maximum,
    )
    messages = (
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=task_description),
    )

    response = send_chat_completion(orchestrator_llm, messages)
    raw_text = _extract_text(response.message)

    parsed = _parse_json_response(raw_text)
    raw_sub_tasks = parsed.get("sub_tasks", [])
    if not isinstance(raw_sub_tasks, list) or not raw_sub_tasks:
        raise OrchestratorPlanError(
            f"Orchestrator returned invalid plan: {raw_text[:500]}"
        )

    max_subtasks: int = settings.ORCHESTRATOR_MAX_SUBTASKS
    raw_sub_tasks = raw_sub_tasks[:max_subtasks]

    sub_tasks = tuple(
        _build_sub_task(item, bounds)
        for item in raw_sub_tasks
        if isinstance(item, dict)
    )

    for i, st in enumerate(sub_tasks, 1):
        _log(
            on_log,
            f"[Orchestrator]   Sub-task {i} (budget {st.timeout_seconds}s): "
            f"{st.description}",
        )

    return sub_tasks


def _build_sub_task(item: dict[str, object], bounds: TimeoutBounds) -> SubTask:
    return SubTask(
        description=str(item.get("description", "")),
        expected_result=str(item.get("expected_result", "")),
        timeout_seconds=resolve_timeout_seconds(item.get("timeout_seconds"), bounds),
    )


def _config_for_sub_task(base: AgentConfig, sub_task: SubTask) -> AgentConfig:
    return replace(base, timeout_seconds=sub_task.timeout_seconds)


def _execute_sub_tasks(
    *,
    orchestrator_llm: LLMConfig,
    progress: _Progress,
    project_id: int,
    sub_agent_config: AgentConfig,
    bounds: TimeoutBounds,
    resuming: bool,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
    on_log: LogCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
) -> OrchestratorResult:
    sub_tasks = progress.plan
    attempts = progress.attempts
    state_lines = progress.state_lines
    browser_selection = progress.browser_selection
    recovery_counts: dict[int, int] = {}
    max_recovery: int = settings.ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS

    evaluate_messages: list[ChatMessage] = [
        ChatMessage(role="system", content=build_evaluate_system_prompt()),
    ]

    total = len(sub_tasks)
    i = progress.next_step - 1

    # Every step already had an attempt: there is nothing to restore or run.
    if resuming and 0 < i < total:
        _check_cancelled(cancellation_check, on_log)
        _restore_resumed_state(
            progress=progress,
            project_id=project_id,
            sub_agent_config=sub_agent_config,
            bounds=bounds,
            system_info=system_info,
            project_prompt=project_prompt,
            on_log=on_log,
        )

    while i < len(sub_tasks):
        _check_cancelled(cancellation_check, on_log)
        sub_task = sub_tasks[i]
        step_num = i + 1
        state_description = "\n".join(state_lines) if state_lines else ""

        # The cursor points at this step before it runs, so a case interrupted
        # mid-step resumes by running it again.
        progress.next_step = step_num
        progress.checkpoint()

        _log(
            on_log,
            f"[Orchestrator] Sub-task {step_num}/{total}: {sub_task.description}",
        )

        result = run_sub_agent(
            sub_task,
            state_description,
            project_id,
            config=_config_for_sub_task(sub_agent_config, sub_task),
            browser_selection=browser_selection,
            system_info=system_info,
            project_prompt=project_prompt,
        )
        progress.total_iterations += result.iterations
        attempts.append(SubTaskAttempt(step_num=step_num, kind="step", result=result))

        _log(
            on_log,
            f"[Orchestrator]   Result: {result.status.upper()} — {result.summary}",
        )

        if result.status == "pass":
            state_lines.append(f"Step {step_num}: {result.summary}")
            progress.next_step = step_num + 1
            progress.checkpoint()
            i += 1
            continue

        # What runs next depends on the failure decision below; until it is made
        # the cursor stays on this step, so a resume reruns it.
        progress.checkpoint()

        remaining = total - step_num
        decision = _evaluate_failure(
            orchestrator_llm=orchestrator_llm,
            evaluate_messages=evaluate_messages,
            sub_task=sub_task,
            sub_task_result=result,
            state_description=state_description,
            remaining_tasks=remaining,
            bounds=bounds,
            on_log=on_log,
        )

        if decision.action == "continue":
            state_lines.append(f"Step {step_num}: FAILED — {result.summary}")
            progress.next_step = step_num + 1
            progress.checkpoint()
            i += 1
            continue

        recovered = _attempt_recovery(
            decision=decision,
            step_index=i,
            step_num=step_num,
            state_description=state_description,
            project_id=project_id,
            sub_agent_config=sub_agent_config,
            progress=progress,
            system_info=system_info,
            project_prompt=project_prompt,
            recovery_counts=recovery_counts,
            max_recovery=max_recovery,
            on_log=on_log,
        )

        if recovered:
            continue

        _log(on_log, f"[Orchestrator]   Stopping: {decision.reason}")
        state_lines.append(f"Step {step_num}: FAILED (stopped) — {result.summary}")
        # The case is decided (Failed): a resume only rebuilds the verdict and
        # never reruns the failed step.
        progress.next_step = total + 1
        progress.checkpoint()
        break

    return _build_verdict(orchestrator_llm, attempts, total, progress.total_iterations)


def _attempt_recovery(
    *,
    decision: OrchestratorDecision,
    step_index: int,
    step_num: int,
    state_description: str,
    project_id: int,
    sub_agent_config: AgentConfig,
    progress: _Progress,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    recovery_counts: dict[int, int],
    max_recovery: int,
    on_log: LogCallback | None,
) -> bool:
    if decision.action != "recover":
        return False

    recoveries = recovery_counts.get(step_index, 0)
    if recoveries >= max_recovery:
        _log(on_log, "[Orchestrator]   Max recovery attempts reached — stopping test.")
        return False

    if decision.recovery_task is None:
        _log(
            on_log,
            "[Orchestrator]   Recovery requested but no task provided — stopping test.",
        )
        return False

    recovery_counts[step_index] = recoveries + 1

    _log(
        on_log,
        f"[Orchestrator]   Recovery (budget {decision.recovery_task.timeout_seconds}s): "
        f"{decision.recovery_task.description}",
    )
    recovery_result = run_sub_agent(
        decision.recovery_task,
        state_description,
        project_id,
        config=_config_for_sub_task(sub_agent_config, decision.recovery_task),
        browser_selection=progress.browser_selection,
        system_info=system_info,
        project_prompt=project_prompt,
    )
    progress.attempts.append(
        SubTaskAttempt(step_num=step_num, kind="recovery", result=recovery_result)
    )

    _log(
        on_log,
        f"[Orchestrator]   Recovery result: {recovery_result.status.upper()} "
        f"— {recovery_result.summary}",
    )

    if recovery_result.status == "pass":
        # Only a passed recovery's iterations count, as before; the step it
        # recovered runs next (cursor unchanged).
        progress.total_iterations += recovery_result.iterations
        progress.state_lines.append(
            f"Recovery for step {step_num}: {recovery_result.summary}"
        )
        progress.checkpoint()
        return True

    progress.checkpoint()
    _log(on_log, "[Orchestrator]   Recovery failed — stopping test.")
    return False


def _build_verdict(
    orchestrator_llm: LLMConfig,
    attempts: list[SubTaskAttempt],
    planned_steps: int,
    total_iterations: int,
) -> OrchestratorResult:
    # A step that failed, was recovered and then passed on retry counts as passed:
    # only each planned step's final attempt decides, recoveries never do.
    finals = _final_step_results(attempts)
    all_passed = len(finals) == planned_steps and all(
        r.status == "pass" for r in finals.values()
    )
    overall_status: Literal["pass", "fail"] = "pass" if all_passed else "fail"
    failed_steps = [num for num, r in finals.items() if r.status == "fail"]
    error = finals[max(failed_steps)].summary if failed_steps else None

    attempts_tuple = tuple(attempts)
    verdict_prompt = build_verdict_prompt(attempts_tuple, overall_status, planned_steps)
    verdict_messages = (
        ChatMessage(role="system", content="Summarize the test case execution."),
        ChatMessage(role="user", content=verdict_prompt),
    )
    try:
        verdict_response = send_chat_completion(orchestrator_llm, verdict_messages)
        summary = _extract_text(verdict_response.message)
    except AIServiceError as exc:
        # Pass or fail is already decided from the sub-task results; the LLM only
        # words the summary, so a provider fault here must not block the case.
        logger.warning(
            "Verdict summary failed (%s), using deterministic summary", exc.kind
        )
        summary = _deterministic_summary(attempts_tuple, overall_status, planned_steps)

    return OrchestratorResult(
        status=overall_status,
        summary=summary,
        attempts=attempts_tuple,
        total_iterations=total_iterations,
        error=error,
    )


def _final_step_results(attempts: list[SubTaskAttempt]) -> dict[int, SubTaskResult]:
    return {
        step_num: attempts[index].result
        for step_num, index in final_step_indices(attempts).items()
    }


def _deterministic_summary(
    attempts: tuple[SubTaskAttempt, ...],
    overall_status: Literal["pass", "fail"],
    planned_steps: int,
) -> str:
    lines = [
        f"Test {'passed' if overall_status == 'pass' else 'failed'} "
        f"(AI summary unavailable)."
    ]
    if was_resumed(attempts):
        lines.append("The case was interrupted and resumed.")
    lines.extend(format_attempt_lines(attempts, planned_steps))
    return "\n".join(lines)


def _evaluate_failure(
    *,
    orchestrator_llm: LLMConfig,
    evaluate_messages: list[ChatMessage],
    sub_task: SubTask,
    sub_task_result: SubTaskResult,
    state_description: str,
    remaining_tasks: int,
    bounds: TimeoutBounds,
    on_log: LogCallback | None = None,
) -> OrchestratorDecision:
    prompt = build_evaluate_prompt(
        sub_task,
        sub_task_result,
        state_description,
        remaining_tasks,
        min_timeout_seconds=bounds.minimum,
        max_timeout_seconds=bounds.maximum,
    )
    evaluate_messages.append(ChatMessage(role="user", content=prompt))

    response = send_chat_completion(orchestrator_llm, tuple(evaluate_messages))
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
            recovery_task = _build_sub_task(raw_recovery, bounds)

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

    return AgentResult(
        stop_reason=stop_reason,
        iterations=orchestrator_result.total_iterations,
        messages=messages,
        error=orchestrator_result.error,
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
