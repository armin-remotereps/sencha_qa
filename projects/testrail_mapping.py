from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

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
TESTRAIL_STATUS_FAILED: Final = 5

RESULT_COMMENT_TEXT_MAX: Final = 4000

_PIVOT_STATUS_TO_TESTRAIL: Mapping[str, int] = {
    TestRunTestCaseStatus.SUCCESS: TESTRAIL_STATUS_PASSED,
    TestRunTestCaseStatus.FAILED: TESTRAIL_STATUS_FAILED,
}

_TESTRAIL_STATUS_TO_LABEL: Mapping[int, str] = {
    TESTRAIL_STATUS_PASSED: "Passed",
    TESTRAIL_STATUS_FAILED: "Failed",
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

    Only "success" and "failed" are pushable; every other status (created,
    in progress, cancelled) has no meaningful TestRail equivalent.
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


def format_testrail_elapsed(
    started_at: datetime | None, finished_at: datetime | None
) -> str:
    """Format a TestRail "timespan" string such as "1h 2m 3s".

    Returns "" when either timestamp is missing or the span is under one
    second. Only non-zero units are included.
    """
    if started_at is None or finished_at is None:
        return ""
    total_seconds = int((finished_at - started_at).total_seconds())
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


def build_result_comment(
    *, outcome_label: str, run_name: str, result_text: str, case_url: str
) -> str:
    """Build the TestRail result comment body for one pushed case result."""
    truncated_result_text = result_text.strip()[:RESULT_COMMENT_TEXT_MAX]
    paragraphs = [f"{outcome_label} by Punk Hazard in run “{run_name}”."]
    if truncated_result_text:
        paragraphs.append(truncated_result_text)
    paragraphs.append(f"Details: {case_url}")
    return "\n\n".join(paragraphs)


def comment_hash(comment: str) -> str:
    """SHA-256 hex digest of a comment body, used to detect unchanged pushes."""
    return hashlib.sha256(comment.encode("utf-8")).hexdigest()
