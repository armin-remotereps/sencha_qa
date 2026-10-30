from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from projects.case_step_progress import CaseStepProgress, CaseStepRow
from projects.models import (
    TestCaseData,
    TestCasePriority,
    TestCaseType,
    TestRunTestCaseStatus,
)
from projects.testrail_client import (
    TestRailCase,
    TestRailCaseType,
    TestRailPriority,
    TestRailStep,
)

DEFAULT_TEMPLATE_NAME = "Test Case"
STEPS_TEMPLATE_NAME = "Test Case (Steps)"

_TITLE_MAX = 500
_REFERENCES_MAX = 500
_ESTIMATE_MAX = 50

TESTRAIL_STATUS_PASSED: Final = 1
TESTRAIL_STATUS_BLOCKED: Final = 2
TESTRAIL_STATUS_FAILED: Final = 5

_WHITESPACE = re.compile(r"\s+")
_DECISION_LABELS: Mapping[str, str] = {
    "stop": "Stopped here",
    "continue": "Continued despite failure",
    "recover": "Recovery attempted",
}

_PIVOT_STATUS_TO_TESTRAIL: Mapping[str, int] = {
    TestRunTestCaseStatus.SUCCESS: TESTRAIL_STATUS_PASSED,
    TestRunTestCaseStatus.FAILED: TESTRAIL_STATUS_FAILED,
    TestRunTestCaseStatus.BLOCKED: TESTRAIL_STATUS_BLOCKED,
}

_TESTRAIL_STATUS_TO_LABEL: Mapping[int, str] = {
    TESTRAIL_STATUS_PASSED: "Passed",
    TESTRAIL_STATUS_FAILED: "Failed",
    TESTRAIL_STATUS_BLOCKED: "Blocked",
}

# TestRail spells the lowest priority differently from our choice label.
_PRIORITY_NAME_OVERRIDES: Mapping[str, str] = {
    "1 - Do Not Test": TestCasePriority.DONT_TEST,
}

_TYPE_CHOICE_VALUES = frozenset(str(choice) for choice in TestCaseType.values)
_PRIORITY_CHOICE_VALUES = frozenset(str(choice) for choice in TestCasePriority.values)


def flatten_steps(steps: Sequence[TestRailStep]) -> tuple[str, str]:
    """Turn TestRail's step/expected pairs into numbered steps and expected text."""
    step_lines = [
        f"{index}. {step.content}" for index, step in enumerate(steps, start=1)
    ]
    expected_lines = [
        f"{index}. {step.expected}"
        for index, step in enumerate(steps, start=1)
        if step.expected
    ]
    return "\n\n".join(step_lines), "\n\n".join(expected_lines)


def _truncate(value: str, limit: int) -> str:
    return value[:limit]


@dataclass(frozen=True)
class TestRailFieldMapper:
    type_names: Mapping[int, str]
    priority_names: Mapping[int, str]

    @classmethod
    def from_vocabularies(
        cls,
        case_types: Sequence[TestRailCaseType],
        priorities: Sequence[TestRailPriority],
    ) -> TestRailFieldMapper:
        return cls(
            type_names={case_type.id: case_type.name for case_type in case_types},
            priority_names={priority.id: priority.name for priority in priorities},
        )

    def map_type(self, type_id: int | None) -> str:
        name = self.type_names.get(type_id) if type_id is not None else None
        if name in _TYPE_CHOICE_VALUES:
            return str(name)
        return str(TestCaseType.FUNCTIONAL)

    def map_priority(self, priority_id: int | None) -> str:
        name = self.priority_names.get(priority_id) if priority_id is not None else None
        if name is None:
            return str(TestCasePriority.MUST_TEST_HIGH)
        if name in _PRIORITY_NAME_OVERRIDES:
            return _PRIORITY_NAME_OVERRIDES[name]
        if name in _PRIORITY_CHOICE_VALUES:
            return name
        return str(TestCasePriority.MUST_TEST_HIGH)

    def to_test_case_data(self, case: TestRailCase) -> TestCaseData:
        if case.steps_separated:
            steps, expected = flatten_steps(case.steps_separated)
            template = STEPS_TEMPLATE_NAME
        else:
            steps, expected, template = case.steps, case.expected, DEFAULT_TEMPLATE_NAME
        return TestCaseData(
            title=_truncate(case.title, _TITLE_MAX),
            testrail_id=str(case.id),
            template=template,
            type=self.map_type(case.type_id),
            priority=self.map_priority(case.priority_id),
            estimate=_truncate(case.estimate, _ESTIMATE_MAX),
            references=_truncate(case.refs, _REFERENCES_MAX),
            preconditions=case.preconditions,
            steps=steps,
            expected=expected,
        )


def map_pivot_status_to_testrail(status: str) -> int | None:
    """Map a TestRunTestCase status to the TestRail result status id.

    Only "success", "failed" and "blocked" are pushable; every other status
    (created, in progress, cancelled) has no meaningful TestRail equivalent.
    """
    return _PIVOT_STATUS_TO_TESTRAIL.get(status)


def parse_testrail_case_id(raw: str) -> int | None:
    """Parse a stored TestCase.testrail_id into the bare TestRail case id.

    Accepts "123" (API imports) and a case-insensitive "C123" prefix (XML
    exports). Anything else, including "0" or non-digit text, is None.
    """
    stripped = raw.strip()
    digits = stripped[1:] if stripped[:1].lower() == "c" else stripped
    if not digits.isdigit():
        return None
    case_id = int(digits)
    return case_id if case_id > 0 else None


def _format_duration_component(value: int, unit: str) -> str:
    return f"{value}{unit}" if value else ""


def format_testrail_elapsed(value: timedelta | None) -> str:
    """Format a case's active duration as a TestRail "timespan" such as "1h 2m 3s".

    Returns "" (no ``elapsed`` sent) when the duration is unknown or under one
    second. Only non-zero units are included.
    """
    if value is None:
        return ""
    total_seconds = int(value.total_seconds())
    if total_seconds < 1:
        return ""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    components = [
        _format_duration_component(hours, "h"),
        _format_duration_component(minutes, "m"),
        _format_duration_component(seconds, "s"),
    ]
    return " ".join(component for component in components if component)


def testrail_outcome_label(status_id: int) -> str:
    """Human-readable label for a TestRail result status id."""
    return _TESTRAIL_STATUS_TO_LABEL.get(status_id, "Result")


def pluralize_count(count: int, singular: str, plural: str) -> str:
    """Render as "1 result" or "3 results" depending on the count."""
    return f"{count} {singular if count == 1 else plural}"


def format_push_confirm_body(pending_count: int, testrail_run_id: int) -> str:
    """Body text for the "push to TestRail" confirmation dialog."""
    changed = pluralize_count(pending_count, "result", "results")
    return (
        f"{changed} will be added to TestRail run R{testrail_run_id}. "
        "Unchanged results are skipped."
    )


def format_blocked_result_text(reason_label: str, result_text: str) -> str:
    """Prefix a blocked case's result with its reason, as "<reason>: <result>".

    Either part may be empty; the separator is only added when both exist.
    """
    reason = reason_label.strip()
    text = result_text.strip()
    if reason and text:
        return f"{reason}: {text}"
    return reason or text


def _one_line(text: str) -> str:
    """Collapse whitespace (newlines included) into one line, never shortened."""
    flat = _WHITESPACE.sub(" ", text).strip()
    # Drop leading markup TestRail's Markdown would reinterpret mid-list.
    return flat.lstrip("#>").lstrip()


def _step_lines(row: CaseStepRow) -> str:
    status = (
        f"{row.status_text} (carried over from the earlier plan)"
        if row.carried_from is not None
        else row.status_text
    )
    line = f"{row.num}. {status}: " + _one_line(row.description)
    result = _one_line(row.summary or row.error)
    if result:
        line += f" — {result}"
    if row.decision is not None:
        reason = _one_line(row.decision.reason)
        label = _DECISION_LABELS.get(row.decision.action, "Decision")
        if reason:
            line += f"\n   {label}: {reason}"
    return line


def format_step_breakdown(progress: CaseStepProgress) -> str:
    """Plain-text list of the agent's planned steps with status and result.

    Numbered like the verdict text ("Steps 4–7 were not run"), so a reader can
    see what those steps were. Every step is listed in full. A plan a help
    chat or a result review redesigned says so under the header.
    """
    if not progress.rows:
        return ""
    header = f"Steps planned by the agent ({progress.total}):"
    if any(not plan.is_review for plan in progress.earlier_plans):
        header += "\nThe test plan was redesigned during help."
    if any(plan.is_review for plan in progress.earlier_plans):
        header += "\nThe test plan was redesigned after a result review."
    entries = [_step_lines(row) for row in progress.rows]
    return "\n".join([header, *entries])


def build_result_comment(
    *,
    outcome_label: str,
    run_name: str,
    result_text: str,
    step_breakdown: str,
) -> str:
    """Build the TestRail result comment body for one pushed case result.

    The full result text, then ``step_breakdown`` (from
    ``format_step_breakdown``; empty for none). No length cap and no link back.
    """
    text = result_text.strip()
    paragraphs = [f"{outcome_label} by Punk Hazard in run “{run_name}”."]
    if text:
        paragraphs.append(text)
    if step_breakdown:
        paragraphs.append(step_breakdown)
    return "\n\n".join(paragraphs)


# Stored in place of the hash of results pushed before comments lost their
# caps and link (migration 0044): those are re-sent only on a status change.
LEGACY_COMMENT_HASH: Final = "legacy"


def comment_hash(comment: str) -> str:
    """SHA-256 hex digest of a comment body, used to detect unchanged pushes."""
    return hashlib.sha256(comment.encode("utf-8")).hexdigest()


CHECKED_PUSH_SUMMARY_MAX: Final = 255
_ATTACHMENT_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def build_checked_result_comment(
    *,
    outcome_label: str,
    run_name: str,
    result_text: str,
    step_breakdown: str,
    checked_by_label: str,
    checked_at: datetime,
    screenshot_count: int,
    screenshot_steps: Sequence[int],
) -> str:
    """The 018 result comment plus who checked it and how many screenshots follow.

    `screenshot_steps` are the plan steps the attached screenshots were taken
    in (empty when unknown, e.g. untagged screenshots from older runs).
    """
    paragraphs = [
        build_result_comment(
            outcome_label=outcome_label,
            run_name=run_name,
            result_text=result_text,
            step_breakdown=step_breakdown,
        ),
        f"Checked by {checked_by_label} on {checked_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC.",
    ]
    if screenshot_count > 0:
        shots = pluralize_count(screenshot_count, "screenshot", "screenshots")
        paragraphs.append(f"{shots} attached{_steps_note(screenshot_steps)}.")
    return "\n\n".join(paragraphs)


def _steps_note(steps: Sequence[int]) -> str:
    """e.g. " from step 3" or " from steps 2, 3"; empty when no step is known."""
    numbers = sorted(set(steps))
    if not numbers:
        return ""
    label = "step" if len(numbers) == 1 else "steps"
    return f" from {label} {', '.join(str(number) for number in numbers)}"


def _run_list(run_ids: Sequence[int]) -> str:
    return ", ".join(f"R{run_id}" for run_id in run_ids)


def format_checked_push_summary(
    *,
    sent: int,
    screenshots: int,
    sent_run_ids: Sequence[int],
    already_sent: int,
    not_in_run: Mapping[int, int],
    missing_runs: Sequence[int],
    closed_runs: Sequence[int],
    no_source: int,
    no_case_id: int,
    missing_files: int,
) -> str:
    """e.g. "7 results sent to R123, R456 · 18 screenshots · 1 not in R123",
    or "Nothing sent · R7 closed" when nothing went out.

    Truncated to fit `TestRun.testrail_source_push_summary` (255 chars).
    """
    results = pluralize_count(sent, "result", "results")
    shots = pluralize_count(screenshots, "screenshot", "screenshots")
    # A resumed send may only attach screenshots to results added earlier.
    head = results if sent else shots if screenshots else "Nothing"
    head = f"{head} sent"
    if sent_run_ids:
        head = f"{head} to {_run_list(sent_run_ids)}"
    parts = [head]
    if sent and screenshots:
        parts.append(shots)
    if already_sent:
        parts.append(f"{already_sent} already sent")
    parts.extend(
        f"{count} not in R{run_id}"
        for run_id, count in sorted(not_in_run.items())
        if count
    )
    if missing_runs:
        parts.append(f"{_run_list(missing_runs)} not found")
    if closed_runs:
        parts.append(f"{_run_list(closed_runs)} closed")
    if no_source:
        parts.append(f"{no_source} without a source run")
    if no_case_id:
        parts.append(f"{no_case_id} without a TestRail ID")
    if missing_files:
        parts.append(
            f"{pluralize_count(missing_files, 'screenshot file', 'screenshot files')} missing"
        )
    summary = " · ".join(parts)
    if len(summary) <= CHECKED_PUSH_SUMMARY_MAX:
        return summary
    return summary[: CHECKED_PUSH_SUMMARY_MAX - 1] + "…"


def format_checked_push_confirm_body(
    ready_count: int, run_ids: Sequence[int], screenshot_count: int
) -> str:
    """Body of the "Send checked to TestRail" confirm dialog."""
    if len(run_ids) > 1:
        runs = f"TestRail runs {_run_list(run_ids[:-1])} and R{run_ids[-1]}"
    else:
        runs = f"TestRail run {_run_list(run_ids)}"
    shots = pluralize_count(screenshot_count, "screenshot", "screenshots")
    if ready_count == 0:
        return (
            f"The remaining {shots} will be attached to results already in {runs}. "
            "Attachments cannot be removed from here afterwards."
        )
    results = pluralize_count(ready_count, "result", "results")
    with_shots = f", with {shots}" if screenshot_count else ""
    return (
        f"{results} will be added to {runs}{with_shots}. "
        "Results cannot be removed from TestRail afterwards."
    )


def screenshot_attachment_filename(
    case_id: int, index: int, image_name: str, step_num: int | None
) -> str:
    """Attachment name such as "C123_s03_02_click.png" from a stored screenshot path.

    `index` is 1-based within the case. `step_num` is the plan step the
    screenshot was taken in; when unknown the name has no step part
    ("C123_02_click.png"). Stored names look like "<ms>_<tool>.png"; the tool
    part becomes the label so TestRail lists the shots in a readable order.
    """
    stem, extension = posixpath.splitext(posixpath.basename(image_name))
    tool = stem.split("_", 1)[1] if "_" in stem else stem
    label = _ATTACHMENT_LABEL_UNSAFE.sub("-", tool).strip("-") or "screenshot"
    step = f"s{step_num:02d}_" if step_num is not None else ""
    return f"C{case_id}_{step}{index:02d}_{label}{extension.lower() or '.png'}"
