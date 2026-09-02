from __future__ import annotations

from agents.types import SubTask, SubTaskResult


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
        "Decide whether to continue, recover, or stop."
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
        f"CURRENT STATE:\n{state_description}\n\n"
        f"REMAINING SUB-TASKS: {remaining_tasks}\n\n"
        "Decide what to do:\n"
        '- "continue": skip this failure and proceed to the next sub-task\n'
        '- "recover": create a recovery sub-task to fix the issue, then retry the failed sub-task\n'
        '- "stop": the failure is non-recoverable, fail the entire test case\n\n'
        "If the sub-task timed out while a long operation was still legitimately in progress, "
        "the recovery sub-task should simply resume observing that operation with a larger "
        "timeout_seconds (between "
        f"{min_timeout_seconds} and {max_timeout_seconds}) — do not add verification steps "
        "the test case never asked for.\n\n"
        "OUTPUT FORMAT — respond with ONLY this JSON, no other text:\n"
        '{"decision": "continue"|"recover"|"stop", "reason": "...", '
        '"recovery_task": {"description": "...", "expected_result": "...", '
        '"timeout_seconds": N}}  '
        "(recovery_task only when decision is recover)"
    )


def build_verdict_prompt(sub_task_results: tuple[SubTaskResult, ...]) -> str:
    lines: list[str] = []
    for i, result in enumerate(sub_task_results, 1):
        error_part = f" | Error: {result.error}" if result.error else ""
        lines.append(f"  {i}. [{result.status.upper()}] {result.summary}{error_part}")
    results_text = "\n".join(lines)
    return (
        f"All sub-tasks have been executed. Results:\n{results_text}\n\n"
        "Provide a final summary of the test case execution in 2-4 sentences. "
        "State whether the test case PASSED or FAILED overall, and highlight "
        "any key observations."
    )
