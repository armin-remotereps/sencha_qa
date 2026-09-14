from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from projects.models import TestCaseData, TestCasePriority, TestCaseType
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
