from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from django.conf import settings
from django.utils import timezone

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
    format_failed_attempt_state,
    format_help_state_line,
    format_redesign_state_line,
    help_verdict_note,
    redesign_for_help,
    redesign_verdict_note,
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
    CheckpointHelp,
    DecisionAction,
    FailureCause,
    HelpRequestInfo,
    LLMConfig,
    LogCallback,
    OrchestratorCheckpoint,
    OrchestratorDecision,
    OrchestratorPhase,
    OrchestratorResult,
    PlanRevisionRecord,
    ScreenshotCallback,
    StepDecision,
    SubTask,
    SubTaskAttempt,
    SubTaskResult,
)

logger = logging.getLogger(__name__)

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

_FAILURE_CAUSES: tuple[FailureCause, ...] = (
    "environment",
    "agent",
    "product",
    "unclear",
)


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
    # A person or the setup agent fixed the machine at the cursor step and left
    # it as it is: re-run that step from the current screen, without a restore.
    continuing_after_help = (
        resume is not None and resume.help is not None and resume.help.continue_pending
    )
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
        if continuing_after_help:
            _log(
                on_log,
                "[Orchestrator] Continuing after help: the machine was fixed, "
                "so the state is not restored.",
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
        continuing_after_help=continuing_after_help,
        system_info=system_info,
        project_prompt=project_prompt,
        on_log=on_log,
        cancellation_check=cancellation_check,
    )

    if orchestrator_result.help is not None:
        _log(
            on_log,
            f"[Orchestrator] Paused for help at step "
            f"{orchestrator_result.help.step_num} "
            f"({orchestrator_result.total_iterations} total iterations)",
        )
    else:
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
    # Step -> the failed attempt's state with its actions, shown only while that
    # step is retried after a passed recovery (state_lines keep one line).
    retry_notes: dict[int, str] = field(default_factory=dict)
    total_iterations: int = 0
    # 1-based step that is running or runs next.
    next_step: int = 1
    # Display-only: why the case continued, recovered or stopped after a failed
    # step, and what it is doing at the cursor step since when.
    decisions: list[StepDecision] = field(default_factory=list)
    phase: OrchestratorPhase = "step"
    phase_started_at: datetime | None = None
    # The last help this case got, carried into every checkpoint so a later
    # resume still names it; never marked continue_pending once running.
    help: CheckpointHelp | None = None
    # Help-chat redesigns applied to this case and the newest earlier plans;
    # carried into every checkpoint so a save after a resume keeps them.
    plan_version: int = 0
    plan_revisions: tuple[PlanRevisionRecord, ...] = ()

    def set_phase(self, phase: OrchestratorPhase) -> None:
        # Every call enters a new phase (a new step is a new "step" phase), so
        # the start time is always restamped.
        self.phase = phase
        self.phase_started_at = timezone.now()
        self.checkpoint()

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
                decisions=tuple(self.decisions),
                phase=self.phase,
                phase_started_at=self.phase_started_at,
                help=self.help,
                plan_version=self.plan_version,
                plan_revisions=self.plan_revisions,
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
    progress.decisions = list(resume.decisions)
    progress.plan_version = resume.plan_version
    progress.plan_revisions = resume.plan_revisions
    # The first checkpoint below already clears continue_pending: an
    # interruption after it resumes normally, with a restore.
    progress.help = (
        None if resume.help is None else replace(resume.help, continue_pending=False)
    )
    progress.state_lines, progress.retry_notes = _rebuild_state(
        resume.attempts,
        resume.next_step,
        resume.help,
        resume.decisions,
        resume.plan_revisions,
    )
    return progress


def _rebuild_state(
    attempts: tuple[SubTaskAttempt, ...],
    next_step: int,
    help: CheckpointHelp | None = None,
    decisions: Sequence[StepDecision] = (),
    revisions: Sequence[PlanRevisionRecord] = (),
) -> tuple[list[str], dict[int, str]]:
    """Rebuild the state lines and retry notes ``_execute_sub_tasks`` wrote.

    A failed step got a plain line when the run moved past it ("continue"); one
    followed by a passed recovery got the failed-attempt line just before the
    recovery line, plus a retry note with its actions; one still at the cursor
    got none. The failed attempt the case paused at for ``help`` gets the
    failed-attempt line, the help line and a retry note, as a passed recovery
    does.

    When that help redesigned the test plan, the failed attempt belongs to the
    earlier plan (in the newest revision record): the carried steps get their
    lines, then the redesign line and the help line, and the first new step
    that is not carried gets the earlier failed attempt as its retry note.
    """
    redesign = redesign_for_help(help, revisions)
    final_index = final_step_indices(attempts)
    help_anchor = _help_anchor_index(
        attempts, help, next_step, final_index, redesigned=redesign is not None
    )
    last_failed: dict[int, SubTaskResult] = {}
    lines: list[str] = []
    notes: dict[int, str] = {}
    # Carried attempts lead the attempts; the redesign lines follow them.
    redesign_pending = redesign is not None
    for index, attempt in enumerate(attempts):
        result = attempt.result
        if redesign_pending and attempt.carried_from is None:
            lines.extend(_redesign_state(redesign, help, notes))
            redesign_pending = False
        if attempt.carried_from is not None and result.status == "pass":
            lines.append(
                f"Step {attempt.step_num}: {result.summary} (done before the plan "
                f"was redesigned; earlier step {attempt.carried_from})"
            )
        elif attempt.kind == "step":
            if result.status != "pass":
                last_failed[attempt.step_num] = result
            if help is not None and index == help_anchor:
                lines.append(
                    format_failed_attempt_state(
                        attempt.step_num, result, with_actions=False
                    )
                )
                lines.append(
                    format_help_state_line(
                        attempt.step_num,
                        help,
                        _help_reason(decisions, attempt.step_num),
                    )
                )
                notes[attempt.step_num] = format_failed_attempt_state(
                    attempt.step_num, result, with_actions=True
                )
            elif result.status == "pass":
                lines.append(f"Step {attempt.step_num}: {result.summary}")
            elif (
                final_index[attempt.step_num] == index and attempt.step_num < next_step
            ):
                lines.append(f"Step {attempt.step_num}: FAILED — {result.summary}")
        elif result.status != "pass":
            continue
        elif attempt.kind == "recovery":
            failed = last_failed.get(attempt.step_num)
            if failed is not None:
                lines.append(
                    format_failed_attempt_state(
                        attempt.step_num, failed, with_actions=False
                    )
                )
                notes[attempt.step_num] = format_failed_attempt_state(
                    attempt.step_num, failed, with_actions=True
                )
            lines.append(f"Recovery for step {attempt.step_num}: {result.summary}")
        else:
            lines.append(_resume_state_line(attempt.step_num, result.summary))
    if redesign_pending:
        lines.extend(_redesign_state(redesign, help, notes))
    return lines, notes


def _redesign_state(
    record: PlanRevisionRecord | None,
    help: CheckpointHelp | None,
    notes: dict[int, str],
) -> list[str]:
    """The redesign and help lines; sets the first new step's retry note.

    The retry note is the earlier plan's failed attempt at the step the case
    paused at, with its actions, so the first new step knows what was done to
    the screen.
    """
    if record is None or help is None:
        return []
    label = f"Earlier plan, step {record.paused_step}"
    failed = _earlier_failed_result(record)
    if failed is not None:
        notes[record.resume_step] = format_failed_attempt_state(
            record.paused_step,
            replace(failed, actions_digest=record.paused_digest),
            with_actions=True,
            step_label=label,
        )
    return [
        format_redesign_state_line(record),
        format_help_state_line(
            record.paused_step,
            help,
            _help_reason(record.earlier_decisions, record.paused_step),
            step_label=label,
        ),
    ]


def _earlier_failed_result(record: PlanRevisionRecord) -> SubTaskResult | None:
    """The earlier plan's last failed attempt at the step the case paused at."""
    for attempt in reversed(record.earlier_attempts):
        if (
            attempt.kind == "step"
            and attempt.step_num == record.paused_step
            and attempt.result.status != "pass"
        ):
            return attempt.result
    return None


def _help_anchor_index(
    attempts: tuple[SubTaskAttempt, ...],
    help: CheckpointHelp | None,
    next_step: int,
    final_index: dict[int, int],
    *,
    redesigned: bool = False,
) -> int | None:
    """Index of the failed step attempt the case paused at for ``help``.

    That is the last failed attempt of the helped step that was retried
    afterwards, or that is still at the cursor waiting for its retry. None
    when the attempt is gone (a re-run in place dropped it), or when the help
    ``redesigned`` the plan: the attempt then belongs to the earlier plan, and
    ``help.step_num`` names an earlier step, not one of the current plan.
    """
    if help is None or redesigned:
        return None
    anchor: int | None = None
    for index, attempt in enumerate(attempts):
        if (
            attempt.kind == "step"
            and attempt.step_num == help.step_num
            and attempt.result.status != "pass"
            and (
                final_index[attempt.step_num] != index or attempt.step_num == next_step
            )
        ):
            anchor = index
    return anchor


def _help_reason(decisions: Sequence[StepDecision], step_num: int) -> str:
    """Why the orchestrator last asked for help at ``step_num``."""
    for decision in reversed(decisions):
        if decision.step_num == step_num and decision.action == "need_help":
            return decision.reason
    return ""


def _applied_help(
    help: CheckpointHelp | None,
    attempts: Sequence[SubTaskAttempt],
    decisions: Sequence[StepDecision],
    revisions: Sequence[PlanRevisionRecord] = (),
) -> CheckpointHelp | None:
    """``help`` when its step's failed attempt is still in the attempts.

    Also when the help redesigned the test plan: that attempt then moved into
    the newest revision record. A help without an ask gets the need_help
    decision's reason as its ask, as the state line does.
    """
    if help is None:
        return None
    redesign = redesign_for_help(help, revisions)
    if redesign is not None:
        if help.ask.strip():
            return help
        return replace(
            help, ask=_help_reason(redesign.earlier_decisions, help.step_num)
        )
    for attempt in attempts:
        if (
            attempt.kind == "step"
            and attempt.step_num == help.step_num
            and attempt.result.status != "pass"
        ):
            if help.ask.strip():
                return help
            return replace(help, ask=_help_reason(decisions, help.step_num))
    return None


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
    progress.set_phase("resume")
    started = time.monotonic()
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
        SubTaskAttempt(
            step_num=step_num,
            kind="resume",
            result=result,
            duration_seconds=_elapsed_seconds(started),
        )
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
    continuing_after_help: bool = False,
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
    # After help the machine is exactly as the person or setup agent left it.
    if resuming and not continuing_after_help and 0 < i < total:
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
        # A step retried after a passed recovery also sees what its failed
        # attempt did.
        state_description = "\n".join(
            [*state_lines, progress.retry_notes[step_num]]
            if step_num in progress.retry_notes
            else state_lines
        )

        # The cursor points at this step before it runs, so a case interrupted
        # mid-step resumes by running it again.
        progress.next_step = step_num
        progress.set_phase("step")

        _log(
            on_log,
            f"[Orchestrator] Sub-task {step_num}/{total}: {sub_task.description}",
        )

        started = time.monotonic()
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
        attempts.append(
            SubTaskAttempt(
                step_num=step_num,
                kind="step",
                result=result,
                duration_seconds=_elapsed_seconds(started),
            )
        )

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
        progress.set_phase("evaluating")

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
        progress.decisions.append(
            StepDecision(
                step_num=step_num, action=decision.action, reason=decision.reason
            )
        )

        if decision.action == "need_help":
            return _pause_for_help(
                progress,
                step_num=step_num,
                reason=decision.reason,
                ask=decision.help_ask,
                failed_result=result,
                on_log=on_log,
            )

        if decision.action == "continue":
            state_lines.append(f"Step {step_num}: FAILED — {result.summary}")
            progress.next_step = step_num + 1
            progress.checkpoint()
            i += 1
            continue

        recovery = _attempt_recovery(
            decision=decision,
            step_index=i,
            step_num=step_num,
            state_description=state_description,
            failed_result=result,
            project_id=project_id,
            sub_agent_config=sub_agent_config,
            progress=progress,
            system_info=system_info,
            project_prompt=project_prompt,
            recovery_counts=recovery_counts,
            max_recovery=max_recovery,
            on_log=on_log,
        )

        if recovery.recovered:
            continue

        if decision.action == "recover":
            # A recovery that cannot go ahead for a problem the application is
            # not known to cause goes to a person instead of failing the case.
            if decision.cause != "product":
                reason = f"{recovery.failure}: {decision.reason}"
                progress.decisions.append(
                    StepDecision(step_num=step_num, action="need_help", reason=reason)
                )
                _log(on_log, f"[Orchestrator]   Escalating to help: {reason}")
                return _pause_for_help(
                    progress,
                    step_num=step_num,
                    reason=reason,
                    ask=decision.help_ask,
                    failed_result=result,
                    on_log=on_log,
                )
            _record_stop(progress, step_num, recovery.failure)

        _log(on_log, f"[Orchestrator]   Stopping: {decision.reason}")
        state_lines.append(f"Step {step_num}: FAILED (stopped) — {result.summary}")
        # The case is decided (Failed): a resume only rebuilds the verdict and
        # never reruns the failed step. The verdict checkpoint below persists it.
        progress.next_step = total + 1
        break

    progress.set_phase("verdict")
    return _build_verdict(
        orchestrator_llm,
        attempts,
        sub_tasks,
        progress.total_iterations,
        helped=_applied_help(
            progress.help, attempts, progress.decisions, progress.plan_revisions
        ),
        revisions=progress.plan_revisions,
    )


def _pause_for_help(
    progress: _Progress,
    *,
    step_num: int,
    reason: str,
    ask: str,
    failed_result: SubTaskResult,
    on_log: LogCallback | None,
) -> OrchestratorResult:
    """Stop the case at ``step_num`` for help, without a verdict.

    The cursor stays on the failed step and the awaiting_help checkpoint
    persists it, so the case continues by re-running that step once someone
    has fixed the machine.
    """
    progress.next_step = step_num
    progress.set_phase("awaiting_help")
    help_ask = ask.strip() or (
        f"Check the test machine at step {step_num}: {failed_result.summary}"
    )
    _log(on_log, f"[Orchestrator]   Needs help at step {step_num}: {help_ask}")
    return OrchestratorResult(
        status="need_help",
        summary=f"Paused at step {step_num} for help: {reason}",
        attempts=tuple(progress.attempts),
        total_iterations=progress.total_iterations,
        help=HelpRequestInfo(step_num=step_num, reason=reason, ask=help_ask),
    )


@dataclass(frozen=True)
class _RecoveryOutcome:
    recovered: bool
    # Why a recover decision could not go ahead; empty when it did not apply.
    failure: str = ""


def _attempt_recovery(
    *,
    decision: OrchestratorDecision,
    step_index: int,
    step_num: int,
    state_description: str,
    failed_result: SubTaskResult,
    project_id: int,
    sub_agent_config: AgentConfig,
    progress: _Progress,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    recovery_counts: dict[int, int],
    max_recovery: int,
    on_log: LogCallback | None,
) -> _RecoveryOutcome:
    if decision.action != "recover":
        return _RecoveryOutcome(recovered=False)

    recoveries = recovery_counts.get(step_index, 0)
    if recoveries >= max_recovery:
        _log(on_log, "[Orchestrator]   Max recovery attempts reached.")
        return _RecoveryOutcome(
            recovered=False, failure="Max recovery attempts reached"
        )

    if decision.recovery_task is None:
        _log(on_log, "[Orchestrator]   Recovery requested but no task provided.")
        return _RecoveryOutcome(
            recovered=False, failure="Recovery requested but no task provided"
        )

    recovery_counts[step_index] = recoveries + 1

    _log(
        on_log,
        f"[Orchestrator]   Recovery (budget {decision.recovery_task.timeout_seconds}s): "
        f"{decision.recovery_task.description}",
    )
    # The recovery, and on success the retried step, learn what the failed
    # attempt already did.
    failed_note = format_failed_attempt_state(
        step_num, failed_result, with_actions=True
    )
    recovery_state = (
        f"{state_description}\n{failed_note}" if state_description else failed_note
    )
    progress.set_phase("recovery")
    started = time.monotonic()
    recovery_result = run_sub_agent(
        decision.recovery_task,
        recovery_state,
        project_id,
        config=_config_for_sub_task(sub_agent_config, decision.recovery_task),
        browser_selection=progress.browser_selection,
        system_info=system_info,
        project_prompt=project_prompt,
    )
    progress.attempts.append(
        SubTaskAttempt(
            step_num=step_num,
            kind="recovery",
            result=recovery_result,
            duration_seconds=_elapsed_seconds(started),
        )
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
            format_failed_attempt_state(step_num, failed_result, with_actions=False)
        )
        progress.retry_notes[step_num] = failed_note
        progress.state_lines.append(
            f"Recovery for step {step_num}: {recovery_result.summary}"
        )
        progress.checkpoint()
        return _RecoveryOutcome(recovered=True)

    progress.checkpoint()
    _log(on_log, "[Orchestrator]   Recovery failed.")
    return _RecoveryOutcome(recovered=False, failure="Recovery failed")


def _record_stop(progress: _Progress, step_num: int, reason: str) -> None:
    # A recover decision that cannot go ahead for a product defect ends the
    # case: record the final stop so the step shows why it stopped. The
    # caller's next checkpoint persists it.
    progress.decisions.append(
        StepDecision(step_num=step_num, action="stop", reason=reason)
    )


def _elapsed_seconds(started: float) -> int:
    return int(round(time.monotonic() - started))


def _build_verdict(
    orchestrator_llm: LLMConfig,
    attempts: list[SubTaskAttempt],
    plan: Sequence[SubTask],
    total_iterations: int,
    *,
    helped: CheckpointHelp | None = None,
    revisions: Sequence[PlanRevisionRecord] = (),
) -> OrchestratorResult:
    # A step that failed, was recovered and then passed on retry counts as passed:
    # only each planned step's final attempt decides, recoveries never do.
    finals = _final_step_results(attempts)
    all_passed = len(finals) == len(plan) and all(
        r.status == "pass" for r in finals.values()
    )
    overall_status: Literal["pass", "fail"] = "pass" if all_passed else "fail"
    failed_steps = [num for num, r in finals.items() if r.status == "fail"]
    error = finals[max(failed_steps)].summary if failed_steps else None

    attempts_tuple = tuple(attempts)
    verdict_prompt = build_verdict_prompt(
        attempts_tuple, overall_status, plan, helped=helped, revisions=revisions
    )
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
        summary = _deterministic_summary(
            attempts_tuple, overall_status, plan, helped=helped, revisions=revisions
        )

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
    plan: Sequence[SubTask],
    *,
    helped: CheckpointHelp | None = None,
    revisions: Sequence[PlanRevisionRecord] = (),
) -> str:
    lines = [
        f"Test {'passed' if overall_status == 'pass' else 'failed'} "
        f"(AI summary unavailable)."
    ]
    if was_resumed(attempts):
        lines.append("The case was interrupted and resumed.")
    redesign = redesign_for_help(helped, revisions)
    if helped is not None:
        lines.append(help_verdict_note(helped, redesigned=redesign is not None))
    if redesign is not None:
        lines.append(redesign_verdict_note(redesign))
    lines.extend(format_attempt_lines(attempts, plan, revisions))
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

    action: DecisionAction
    if raw_action == "continue":
        action = "continue"
    elif raw_action == "recover":
        action = "recover"
    elif raw_action == "need_help":
        action = "need_help"
    else:
        action = "stop"
    cause = _parse_cause(parsed.get("cause"))
    help_ask = (
        str(parsed.get("help_ask") or "").strip()
        if action in ("need_help", "recover")
        else ""
    )

    _log(
        on_log,
        f"[Orchestrator]   Decision: {action} (cause: {cause or 'unknown'}) — {reason}",
    )

    recovery_task: SubTask | None = None
    if action == "recover":
        raw_recovery = parsed.get("recovery_task")
        if isinstance(raw_recovery, dict):
            recovery_task = _build_sub_task(raw_recovery, bounds)

    return OrchestratorDecision(
        action=action,
        reason=reason,
        recovery_task=recovery_task,
        cause=cause,
        help_ask=help_ask,
    )


def _parse_cause(value: object) -> FailureCause | None:
    for cause in _FAILURE_CAUSES:
        if value == cause:
            return cause
    return None


def _to_agent_result(orchestrator_result: OrchestratorResult) -> AgentResult:
    if orchestrator_result.status == "pass":
        stop_reason = AgentStopReason.TASK_COMPLETE
    elif orchestrator_result.status == "need_help":
        stop_reason = AgentStopReason.NEEDS_HUMAN_HELP
    else:
        stop_reason = AgentStopReason.ERROR

    messages = (ChatMessage(role="assistant", content=orchestrator_result.summary),)

    return AgentResult(
        stop_reason=stop_reason,
        iterations=orchestrator_result.total_iterations,
        messages=messages,
        error=orchestrator_result.error,
        help=orchestrator_result.help,
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
