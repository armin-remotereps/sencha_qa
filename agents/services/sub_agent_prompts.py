from __future__ import annotations

from agents.services.prompt_parts import (
    build_agent_persona,
    build_app_lifecycle_rules,
    build_environment_context,
    build_qa_rules,
    build_tool_guidelines,
    build_tool_taxonomy,
)
from controller_client.protocol import BROWSER_DISPLAY_NAMES, BrowserName


def build_sub_agent_system_prompt(
    sub_task_description: str,
    expected_result: str,
    state_description: str,
    *,
    timeout_seconds: int,
    current_browser: BrowserName,
    system_info: dict[str, object] | None = None,
    project_prompt: str | None = None,
    tool_names: frozenset[str] | None = None,
) -> str:
    """``tool_names`` set: describe only those tools (a housekeeping sub-task)."""
    if tool_names is None:
        sections = [
            _build_sub_agent_persona(system_info=system_info),
            build_qa_rules(),
            build_tool_taxonomy(),
            build_environment_context(system_info=system_info),
            build_tool_guidelines(),
            _build_current_browser_section(current_browser),
            _build_time_budget_section(timeout_seconds),
            _build_result_format_instructions(),
        ]
    else:
        # No environment section: it steers towards browser, typing and
        # key_press tools a restricted sub-task cannot call.
        sections = [
            _build_sub_agent_persona(system_info=system_info),
            _build_restricted_tools_section(tool_names),
            _build_time_budget_section(timeout_seconds),
            _build_result_format_instructions(),
        ]
    if project_prompt:
        sections.append(_build_project_context(project_prompt))
    sections.append(_build_state_section(state_description))
    sections.append(_build_task_section(sub_task_description, expected_result))
    return "\n\n".join(sections)


def _build_sub_agent_persona(
    *,
    system_info: dict[str, object] | None = None,
) -> str:
    return build_agent_persona(
        system_info=system_info,
        role="a strict QA step executor",
        job="execute ONE test step exactly as described and report the result honestly",
    )


def _build_restricted_tools_section(tool_names: frozenset[str]) -> str:
    return (
        f"TOOLS:\nYou may use only these tools: {', '.join(sorted(tool_names))}. "
        "Any other tool is refused.\n\n"
        f"{build_app_lifecycle_rules()}"
    )


def _build_current_browser_section(current_browser: BrowserName) -> str:
    return (
        f"Current browser for this case: {BROWSER_DISPLAY_NAMES[current_browser]} "
        f"({current_browser.value}). Browser tools keep using it; do not pick a "
        "different browser unless this step explicitly asks for one."
    )


def _build_time_budget_section(timeout_seconds: int) -> str:
    return (
        f"TIME BUDGET:\nYou have {timeout_seconds} seconds of wall-clock time for this "
        "step, counted from your first action. The step fails automatically when the "
        "budget runs out, even if the action itself succeeded. Leave time for a final "
        "screenshot and your RESULT answer; the wait tool shortens any wait that would "
        "run past the budget."
    )


def _build_result_format_instructions() -> str:
    return (
        "RESULT FORMAT:\n"
        "When you are done executing the step, respond with EXACTLY this format "
        "(no tool calls, just text):\n"
        "RESULT: PASS or FAIL\n"
        "SUMMARY: <1-3 sentences describing what happened>\n\n"
        "If you cannot execute the step, respond with FAIL and explain why in the SUMMARY."
    )


def _build_state_section(state_description: str) -> str:
    if not state_description:
        return (
            "CURRENT STATE:\nThis is the first step. No prior actions have been taken."
        )
    return (
        "CURRENT STATE — the machine is in the state resulting from these prior actions. "
        "Applications, windows, and terminals mentioned below are still open and available "
        "unless a summary says they were closed. "
        "Take a screenshot first to see the current screen before acting:\n"
        f"{state_description}"
    )


def _build_project_context(project_prompt: str) -> str:
    return (
        "ENVIRONMENT STATE — this describes what is ALREADY set up on the target "
        "machine. Use paths, credentials, and facts from here instead of guessing "
        "or re-doing setup that is already done:\n"
        "---\n"
        f"{project_prompt}\n"
        "---"
    )


def _build_task_section(sub_task_description: str, expected_result: str) -> str:
    return (
        f"YOUR TASK:\n{sub_task_description}\n\n" f"EXPECTED RESULT:\n{expected_result}"
    )
