from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from agents.types import (
    CheckpointHelp,
    PlanRevisionRecord,
    SubTask,
    SubTaskAttempt,
    SubTaskResult,
)

# Step descriptions in the verdict input are shortened to this many characters:
# enough to name the step, not to repeat it.
_STEP_DESCRIPTION_MAX_CHARS = 100

# The planner ends a step with this housekeeping clause; the teardown of a case
# that never reached the step finds it by the same words.
CLOSE_CLAUSE_PREFIX: Final = "Afterwards, close"


def build_plan_system_prompt(
    project_prompt: str | None = None,
    *,
    min_timeout_seconds: int,
    max_timeout_seconds: int,
) -> str:
    prompt = (
        "You are a QA test orchestrator. Your job is to decompose test cases into "
        "small, focused sub-tasks that an executor agent will run one at a time.\n\n"
    )

    if project_prompt:
        prompt += (
            "ENVIRONMENT STATE — this describes what is ALREADY set up on the target "
            "machine. This is authoritative; trust it over the test case preconditions:\n"
            "---\n"
            f"{project_prompt}\n"
            "---\n\n"
        )

    prompt += (
        "RULES:\n"
        "- Each sub-task must be a single, concrete action with a verifiable expected result.\n"
        "- If a test step is compound (multiple actions), split it into separate sub-tasks.\n"
        "- Add implicit steps when needed (e.g., opening a browser before navigating to a URL).\n"
        "- CRITICAL: Each sub-task is executed by a SEPARATE agent with NO memory of prior steps. "
        "Every sub-task description must be FULLY self-contained — always specify WHICH "
        "application, window, or terminal to use (e.g., 'In the VS Code integrated terminal, "
        "run...' NOT just 'Run...'). If a prior step opened an application, subsequent steps "
        "must reference that application explicitly (e.g., 'In the already-open VS Code "
        "window, ...').\n"
        "- If the test case names a browser (e.g. 'in Firefox', 'using Edge', 'in "
        "Safari'), keep that browser name in the description of the first sub-task that "
        "opens a web page.\n"
        "- CLOSING APPS: when a desktop application the case opened is not used by any "
        "later sub-task, end the description of the LAST sub-task that uses it with "
        f"'{CLOSE_CLAUSE_PREFIX} <App> with close_app (housekeeping, does not affect "
        "this step's result).' This includes the final sub-task of the case, so apps do "
        "not leak into the next case. When the application also hosts the Test Runner "
        "(its terminal, e.g. a Terminal tab the case opened) or has other tabs still "
        f"needed, write instead '{CLOSE_CLAUSE_PREFIX} the <App> tab you opened with "
        "close_tab (housekeeping, does not affect this step's result).' Never add a separate "
        "close-only sub-task, and do not schedule closing for the browser used by "
        "browser tools.\n"
    )

    if project_prompt:
        prompt += (
            "- If the test case has preconditions, compare EACH precondition against the "
            "ENVIRONMENT STATE above. SKIP any precondition that is already satisfied "
            "(e.g., a path exists means the repo is already cloned; a login is mentioned "
            "as done means do not redo it). Only create sub-tasks for preconditions that "
            "are NOT covered by the environment state.\n"
            "- When the environment state provides a specific path or value, use that exact "
            "path/value in sub-task descriptions instead of the generic one from the test case.\n"
        )
    else:
        prompt += (
            "- If the test case has preconditions, create sub-tasks for them FIRST.\n"
        )

    prompt += (
        "- IMPORTANT: Each shell command runs in a FRESH, isolated session — 'cd' in one "
        "command does NOT affect the next. Never create a sub-task whose only action is 'cd'. "
        "Instead, use absolute paths in commands, or instruct the executor to use the `cwd` "
        "parameter of execute_command.\n"
        "- Keep descriptions precise and self-contained — the executor only knows what you "
        "write in each sub-task description plus a brief summary of prior step outcomes.\n"
        "- Do NOT include verification-only sub-tasks unless the test case explicitly requires "
        "checking something after an action. Instead, include verification in the expected_result "
        "of the action sub-task.\n"
        f"{_build_timeout_rule(min_timeout_seconds, max_timeout_seconds)}\n"
        "OUTPUT FORMAT — respond with ONLY this JSON, no other text:\n"
        '{"sub_tasks": [{"description": "...", "expected_result": "...", '
        '"timeout_seconds": N}, ...]}'
    )
    return prompt


def _build_timeout_rule(min_timeout_seconds: int, max_timeout_seconds: int) -> str:
    return (
        "- TIME BUDGET: give every sub-task a timeout_seconds — the wall-clock budget the "
        "executor gets for that step, between "
        f"{min_timeout_seconds} and {max_timeout_seconds} seconds. Budget for the SLOW case, "
        "not the typical one. Quick UI actions (open an app, click, type, navigate) need "
        f"the minimum ({min_timeout_seconds}). Steps that wait on external progress "
        "(downloads, installs, builds, test suites, package managers) need several hundred "
        f"seconds and should usually get the maximum ({max_timeout_seconds}). The executor "
        "cannot extend its budget, so a step that runs out of time fails even if the action "
        "succeeded.\n"
    )


def build_evaluate_system_prompt() -> str:
    return (
        "You are a QA test orchestrator evaluating sub-task failures. "
        "Decide whether to continue, recover, ask for help, or stop. Most failures "
        "on a test machine are environmental, not product defects: do not hesitate "
        "to ask for help."
    )


def build_evaluate_prompt(
    sub_task: SubTask,
    sub_task_result: SubTaskResult,
    state_description: str,
    remaining_tasks: int,
    *,
    min_timeout_seconds: int,
    max_timeout_seconds: int,
) -> str:
    return (
        f"Sub-task FAILED.\n\n"
        f"SUB-TASK: {sub_task.description}\n"
        f"EXPECTED: {sub_task.expected_result}\n"
        f"TIME BUDGET GIVEN: {sub_task.timeout_seconds} seconds\n"
        f"RESULT: {sub_task_result.summary}\n"
        f"ERROR: {sub_task_result.error or 'none'}\n\n"
        "ACTIONS THE FAILED ATTEMPT TOOK (tool calls in order, results shortened; "
        "their effects may still be in place):\n"
        f"{sub_task_result.actions_digest or '(none recorded)'}\n\n"
        f"CURRENT STATE:\n{state_description}\n\n"
        f"REMAINING SUB-TASKS: {remaining_tasks}\n\n"
        "Decide what to do:\n"
        '- "continue": skip this failure and proceed to the next sub-task\n'
        '- "recover": create a recovery sub-task to fix the issue, then retry the '
        "failed sub-task\n"
        '- "need_help": the failure is caused by the test machine or environment, not '
        "by the application under test, and a person at the machine can likely fix it "
        "quickly (an install/download/build that timed out or stalled, a "
        "permission/UAC/admin prompt, an unexpected dialog or popup, an element that is "
        "on screen but does not react to clicks, a missing tool or file, a network "
        "hiccup, a slow or frozen machine). The run pauses, someone fixes the machine, "
        "and this sub-task is retried from the current state.\n"
        '- "stop": ONLY when the application under test itself behaves contrary to the '
        "expected result (a real product defect). This fails the entire test case.\n\n"
        'Prefer "need_help" over "stop" whenever you are not sure the application is at '
        "fault: a wrong Failed costs a bug ticket; a Need help costs a person one "
        "minute. A RESULT that starts with ENVIRONMENT: means the executor saw an "
        "environmental obstacle.\n"
        'Use "recover" only when the agent itself can clearly fix it (it clicked the '
        "wrong thing, or a long operation is still legitimately running and needs more "
        "time), and never more than once for the same environmental problem.\n\n"
        "If the sub-task timed out while a long operation was still legitimately in "
        "progress, the recovery sub-task should simply resume observing that operation "
        "with a larger timeout_seconds (between "
        f"{min_timeout_seconds} and {max_timeout_seconds}) — do not add verification "
        "steps the test case never asked for.\n\n"
        "The recovery sub-agent and the retried sub-task see the failed attempt's "
        "actions too. Write the recovery_task so it does not repeat actions whose "
        "effect is already in place (text already typed, forms already submitted, "
        "apps already opened), and say what the attempt already did when that "
        "matters.\n\n"
        "OUTPUT FORMAT — respond with ONLY this JSON, no other text:\n"
        '{"decision": "continue"|"recover"|"need_help"|"stop", "reason": "...", '
        '"cause": "environment"|"agent"|"product"|"unclear", '
        '"help_ask": "<one or two imperative sentences: what to fix on the machine>", '
        '"recovery_task": {"description": "...", "expected_result": "...", '
        '"timeout_seconds": N}}\n'
        "(help_ask only when decision is need_help or recover; recovery_task only "
        "when decision is recover)"
    )


def format_failed_attempt_state(
    step_num: int,
    result: SubTaskResult,
    *,
    with_actions: bool,
    step_label: str = "",
) -> str:
    """State line for a failed step that a recovery follows.

    ``with_actions`` adds the attempt's digest: given only to the recovery and
    the retry right after it, so later steps do not carry it in every prompt.
    Shared by the live loop and the resume rebuild so both write the same state.
    ``step_label`` replaces "Step N" (an earlier plan's step after a redesign).
    """
    label = step_label or f"Step {step_num}"
    line = f"{label}: an earlier attempt FAILED — {result.summary}"
    if not with_actions or not result.actions_digest:
        return line
    actions = "\n".join(
        f"    {digest_line}" for digest_line in result.actions_digest.splitlines()
    )
    return (
        f"{line}\n  Actions that attempt took (their effects may still be in "
        f"place; check the screen before repeating any):\n{actions}"
    )


def helper_name(help: CheckpointHelp) -> str:
    """Who fixed the machine, as the prompts name them.

    The setup agent when its auto-fix reported the machine fixed, also when a
    person then continued the case (during the auto-fix, or after the fix
    failed the run's gates); a person otherwise, noting when the setup agent
    tried first and could not.
    """
    if help.source == "auto_fix" or help.auto_fix_fixed:
        if help.note.strip():
            return "the machine setup agent (a person also added a note)"
        return "the machine setup agent"
    if help.auto_fix_summary.strip():
        return "a person (the machine setup agent could not)"
    return "a person"


def _sentence(text: str) -> str:
    return text.strip().rstrip(".")


def _help_details(help: CheckpointHelp, reason: str) -> str:
    """The ask and the helpers' messages, each as a sentence; may be empty.

    ``reason`` stands in for the ask when the help key has none (older
    checkpoints, or the agent asked without one).
    """
    parts: list[str] = []
    ask = _sentence(help.ask) or _sentence(reason)
    if ask:
        parts.append(f"The agent had asked: {ask}.")
    summary = _sentence(help.auto_fix_summary)
    if summary:
        parts.append(f"Machine setup agent: {summary}.")
    note = _sentence(help.note)
    if note:
        parts.append(f"Person: {note}.")
    return " ".join(parts)


def format_help_state_line(
    step_num: int, help: CheckpointHelp, reason: str, *, step_label: str = ""
) -> str:
    """State line for a step that failed, paused for help and is retried after it.

    ``reason`` is why the orchestrator asked for help at that step, used when
    the help key carries no ask. Shared by the live continue and the resume
    rebuild so both write the same state. ``step_label`` replaces "Step N" (an
    earlier plan's step after a redesign).
    """
    details = _help_details(help, reason)
    return (
        f"{step_label or f'Step {step_num}'}: the test machine was fixed by "
        f"{helper_name(help)}."
        f"{' ' + details if details else ''} Check the screen: the fix may have "
        "changed it."
    )


def help_verdict_note(
    help: CheckpointHelp, reason: str = "", *, redesigned: bool = False
) -> str:
    """Who fixed the machine at which step, with the ask and their messages.

    ``redesigned``: the help also redesigned the test plan, so the step it
    paused at belongs to the earlier plan and was not retried as such.
    """
    details = _help_details(help, reason)
    if redesigned:
        where = f"step {help.step_num} of the earlier plan"
        after = "before the redesigned plan continued"
    else:
        where, after = f"step {help.step_num}", "before the step was retried"
    return (
        f"The case paused at {where} for help, and the test machine was fixed by "
        f"{helper_name(help)} {after}.{' ' + details if details else ''}"
    )


def redesign_for_help(
    help: CheckpointHelp | None, revisions: Sequence[PlanRevisionRecord]
) -> PlanRevisionRecord | None:
    """The newest revision when ``help``'s request redesigned the test plan."""
    if help is None or help.request_id is None or not revisions:
        return None
    latest = revisions[-1]
    return latest if latest.help_request_id == help.request_id else None


def _carried_range(record: PlanRevisionRecord) -> str:
    carried = record.resume_step - 1
    if carried <= 0:
        return "No step was carried over"
    if carried == 1:
        return "Step 1 was carried over"
    return f"Steps 1–{carried} were carried over"


def format_redesign_state_line(record: PlanRevisionRecord) -> str:
    """State line after the carried steps when help redesigned the test plan."""
    reason = _sentence(record.reason)
    return (
        f"The test plan was redesigned during help{': ' + reason if reason else ''}. "
        f"{_carried_range(record)}; continue at step {record.resume_step} from "
        "the current screen."
    )


def redesign_verdict_note(record: PlanRevisionRecord) -> str:
    """Verdict sentence for a help that redesigned the test plan."""
    reason = _sentence(record.reason)
    carried = record.resume_step - 1
    if carried <= 0:
        carried_text = (
            "No step of the earlier plan was carried over; every step ran again"
        )
    else:
        carried_text = (
            f"{_carried_range(record)} from the earlier plan, where they had "
            "passed; they were not re-run"
        )
    return (
        f"The test plan was redesigned during help"
        f"{' (' + reason + ')' if reason else ''}. {carried_text}."
    )


def build_verdict_prompt(
    attempts: tuple[SubTaskAttempt, ...],
    overall_status: Literal["pass", "fail"],
    plan: Sequence[SubTask],
    *,
    helped: CheckpointHelp | None = None,
    revisions: Sequence[PlanRevisionRecord] = (),
) -> str:
    results_text = "\n".join(
        f"  {line}" for line in format_attempt_lines(attempts, plan, revisions)
    )
    outcome = "PASSED" if overall_status == "pass" else "FAILED"
    resumed_note = (
        "The execution was interrupted and resumed: the Resume lines restored the "
        "machine state before continuing, and they do not count either. Mention "
        "that the case was resumed. "
        if was_resumed(attempts)
        else ""
    )
    redesign = redesign_for_help(helped, revisions)
    helped_note = (
        f"{help_verdict_note(helped, redesigned=redesign is not None)} "
        "Mention that help. "
        if helped is not None
        else ""
    )
    redesign_note = (
        f"{redesign_verdict_note(redesign)} Mention this; the carried steps count "
        "as passed. "
        if redesign is not None
        else ""
    )
    return (
        f"Sub-task execution finished. Results:\n{results_text}\n\n"
        "Provide a final summary of the test case execution in 2-4 sentences. "
        f"The overall result is {outcome}. It is decided from the final attempt of "
        "each step, and recovery steps do not count. "
        f"{resumed_note}"
        f"{helped_note}"
        f"{redesign_note}"
        "State that result and do not "
        "contradict it. Highlight any key observations, including recoveries. "
        "Readers never see the numbered step list, so whenever you mention a step "
        'number, name the step briefly next to it, e.g. "Steps 4-7 (open the '
        'build folder, ...) were not run".'
    )


def was_resumed(attempts: Sequence[SubTaskAttempt]) -> bool:
    return any(attempt.kind == "resume" for attempt in attempts)


def format_attempt_lines(
    attempts: tuple[SubTaskAttempt, ...],
    plan: Sequence[SubTask],
    revisions: Sequence[PlanRevisionRecord] = (),
) -> list[str]:
    planned_steps = len(plan)
    final_index = final_step_indices(attempts)
    step_attempt_counts: dict[int, int] = {}
    lines: list[str] = []
    for index, attempt in enumerate(attempts):
        if attempt.kind == "recovery":
            label = f"Recovery for step {attempt.step_num}"
        elif attempt.kind == "resume":
            # A state restore before a resumed case; it is not a run step.
            label = _resume_label(attempt)
        elif attempt.carried_from is not None:
            # Passed under the earlier plan before a help-chat redesign.
            lines.append(_carried_line(attempt, plan, revisions))
            continue
        else:
            count = step_attempt_counts.get(attempt.step_num, 0) + 1
            step_attempt_counts[attempt.step_num] = count
            if final_index[attempt.step_num] != index:
                label = (
                    f"Step {attempt.step_num} (attempt {count}, superseded by retry)"
                )
            elif count > 1:
                label = f"Step {attempt.step_num} (attempt {count}, final)"
            else:
                label = f"Step {attempt.step_num}"
            label = f"{label} — {_short_description(plan, attempt.step_num)}"
        lines.append(_format_result_line(label, attempt.result))

    # Steps run in order from step 1, so the unrun ones are the tail of the plan.
    ran_steps = len(final_index)
    if ran_steps < planned_steps:
        first_unrun = ran_steps + 1
        unrun = (
            f"Step {first_unrun}"
            if first_unrun == planned_steps
            else f"Steps {first_unrun}-{planned_steps}"
        )
        names = "; ".join(
            f"{num}: {_short_description(plan, num)}"
            for num in range(first_unrun, planned_steps + 1)
        )
        lines.append(f"{unrun}: not run — {names}")
    return lines


def _carried_line(
    attempt: SubTaskAttempt,
    plan: Sequence[SubTask],
    revisions: Sequence[PlanRevisionRecord],
) -> str:
    old_step = attempt.carried_from
    old = ""
    # Carried attempts always come from the newest revision.
    if revisions and old_step is not None:
        previous_plan = revisions[-1].previous_plan
        if 1 <= old_step <= len(previous_plan):
            old = f" ({_shorten(previous_plan[old_step - 1][0])})"
    return (
        f"Step {attempt.step_num} ({_short_description(plan, attempt.step_num)}): "
        f"PASSED under the earlier plan as step {old_step}{old}, not re-run — "
        f"{attempt.result.summary}"
    )


def _short_description(plan: Sequence[SubTask], step_num: int) -> str:
    if not 1 <= step_num <= len(plan):
        return "(unknown step)"
    return _shorten(plan[step_num - 1].description)


def _shorten(text: str) -> str:
    description = " ".join(text.split())
    if len(description) <= _STEP_DESCRIPTION_MAX_CHARS:
        return description
    return description[: _STEP_DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"


def final_step_indices(attempts: Sequence[SubTaskAttempt]) -> dict[int, int]:
    """Map each planned step to the index of its final (last) step attempt."""
    return {
        attempt.step_num: index
        for index, attempt in enumerate(attempts)
        if attempt.kind == "step"
    }


def _resume_label(attempt: SubTaskAttempt) -> str:
    if attempt.result.status == "pass":
        return f"Resume: state restored before step {attempt.step_num}"
    return f"Resume: could not restore state before step {attempt.step_num}"


def _format_result_line(label: str, result: SubTaskResult) -> str:
    error_part = f" | Error: {result.error}" if result.error else ""
    return f"{label} [{result.status.upper()}] {result.summary}{error_part}"
