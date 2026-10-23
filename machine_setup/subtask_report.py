"""Parse a setup sub-agent's final report and render it for the main agent.

Pure: reads the tool loop's result, no ORM writes and no settings. Not shared
with the tester's sub-agent parser: that one speaks PASS/FAIL and auto-passes
a missing RESULT, which is a QA rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agents.types import AgentResult, AgentStopReason, ChatMessage
from machine_setup.message_text import message_text
from machine_setup.models import SetupSubtaskResult

REPORT_MAX_CHARS = 8000
SUMMARY_MAX_CHARS = 2000
FACTS_MAX_CHARS = 4000
ERROR_MAX_CHARS = 1500

_HEADER = re.compile(
    r"^[ \t]*[*_#]*[ \t]*(RESULT|SUMMARY|FACTS|ERROR)[ \t]*[*_]*[ \t]*:[*_]*",
    re.IGNORECASE | re.MULTILINE,
)
_RESULTS = {
    "DONE": SetupSubtaskResult.DONE,
    "FAILED": SetupSubtaskResult.FAILED,
    "NEEDS_USER": SetupSubtaskResult.NEEDS_USER,
}
_TRUNCATED = "\n…[truncated]"


@dataclass(frozen=True)
class SubtaskReport:
    result: SetupSubtaskResult
    summary: str
    facts: str
    error: str


def parse_subtask_report(result: AgentResult) -> SubtaskReport:
    """The report in the sub-agent's last text.

    No ``RESULT`` line gives ``UNCLEAR`` with the raw text as summary. A loop
    that ran out of time or steps is ``FAILED`` whatever the text says, with
    the stop reason in ``error``.
    """
    text = _last_assistant_text(result.messages)
    sections = _sections(text)
    outcome = _result_value(sections.get("RESULT", ""))
    summary = sections.get("SUMMARY", "")
    facts = sections.get("FACTS", "")
    error = sections.get("ERROR", "")
    if outcome is None:
        outcome = SetupSubtaskResult.UNCLEAR
        summary = text

    stop_error = _stop_error(result)
    if stop_error is not None:
        outcome = SetupSubtaskResult.FAILED
        if not summary:
            summary = text
        error = stop_error if _is_none(error) else f"{stop_error}\n{error}"

    return SubtaskReport(
        result=outcome,
        summary=_cap(summary, SUMMARY_MAX_CHARS),
        facts=_cap(facts, FACTS_MAX_CHARS),
        error=_cap(error, ERROR_MAX_CHARS),
    )


def render_report_for_main(
    report: SubtaskReport,
    *,
    steps: int,
    elapsed_seconds: int,
    budget_seconds: int,
) -> str:
    """The tool result the main agent reads: a server-written header, then the report."""
    header = (
        f"Sub-task finished: {report.result.value.upper()} · {steps} steps · "
        f"{_format_duration(elapsed_seconds)} of {_format_duration(budget_seconds)}"
    )
    lines = [header]
    if report.result == SetupSubtaskResult.UNCLEAR:
        lines.append(
            "The helper did not use the report format; its last message is the "
            "summary. Verify with a read_only sub-task or ask the user."
        )
    lines.append(f"SUMMARY: {report.summary or 'none'}")
    lines.append(f"FACTS:\n{report.facts}" if report.facts else "FACTS: none")
    lines.append(f"ERROR: {report.error or 'none'}")
    return _cap("\n".join(lines), REPORT_MAX_CHARS)


def _last_assistant_text(messages: tuple[ChatMessage, ...]) -> str:
    for message in reversed(messages):
        if message.role != "assistant":
            continue
        text = message_text(message)
        if text:
            return text
    return ""


def _sections(text: str) -> dict[str, str]:
    """Each header's text up to the next header; the first occurrence wins."""
    sections: dict[str, str] = {}
    matches = list(_HEADER.finditer(text))
    for index, match in enumerate(matches):
        name = match.group(1).upper()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if name not in sections:
            sections[name] = text[match.end() : end].strip()
    return sections


def _result_value(raw: str) -> SetupSubtaskResult | None:
    words = raw.split()
    if not words:
        return None
    key = re.sub(r"[\s-]+", "_", words[0].strip("*_`.,;:").upper())
    if key == "NEEDS" and len(words) > 1:
        key = "NEEDS_" + words[1].strip("*_`.,;:").upper()
    return _RESULTS.get(key)


def _stop_error(result: AgentResult) -> str | None:
    if result.stop_reason == AgentStopReason.TIMEOUT:
        detail = f" ({result.error})" if result.error else ""
        return f"Ran out of time{detail}."
    if result.stop_reason == AgentStopReason.MAX_ITERATIONS:
        return f"Used all {result.iterations} steps."
    return None


def _is_none(text: str) -> bool:
    return text.strip().strip(".").lower() in ("", "none", "n/a")


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATED)] + _TRUNCATED


def _format_duration(seconds: int) -> str:
    minutes, rest = divmod(max(seconds, 0), 60)
    if not minutes:
        return f"{rest}s"
    if not rest:
        return f"{minutes}m"
    return f"{minutes}m{rest}s"
