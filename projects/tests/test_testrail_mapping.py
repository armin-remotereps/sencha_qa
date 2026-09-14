from __future__ import annotations

from django.test import SimpleTestCase

from projects.models import TestCasePriority, TestCaseType
from projects.testrail_client import (
    TestRailCase,
    TestRailCaseType,
    TestRailPriority,
    TestRailStep,
)
from projects.testrail_mapping import (
    DEFAULT_TEMPLATE_NAME,
    STEPS_TEMPLATE_NAME,
    TestRailFieldMapper,
    flatten_steps,
)

CASE_TYPES = [TestRailCaseType(6, "Functional"), TestRailCaseType(11, "Smoke & Sanity")]
PRIORITIES = [
    TestRailPriority(5, "1 - Do Not Test"),
    TestRailPriority(1, "2 - Test If Time"),
    TestRailPriority(4, "5 - Must Test"),
]


def _case(**overrides: object) -> TestRailCase:
    base: dict[str, object] = {
        "id": 338732,
        "title": "Title",
        "template_id": 1,
        "type_id": 6,
        "priority_id": 4,
        "refs": "EXTJS-1",
        "estimate": "5m",
        "preconditions": "pre",
        "steps": "do it",
        "expected": "works",
        "steps_separated": (),
    }
    base.update(overrides)
    return TestRailCase(**base)  # type: ignore[arg-type]  # test-only kwargs bag


class MapperTests(SimpleTestCase):
    def setUp(self) -> None:
        self.mapper = TestRailFieldMapper.from_vocabularies(CASE_TYPES, PRIORITIES)

    def test_basic_fields(self) -> None:
        data = self.mapper.to_test_case_data(_case())
        self.assertEqual(data.testrail_id, "338732")
        self.assertEqual(data.title, "Title")
        self.assertEqual(data.template, DEFAULT_TEMPLATE_NAME)
        self.assertEqual(data.type, TestCaseType.FUNCTIONAL)
        self.assertEqual(data.priority, TestCasePriority.MUST_TEST_HIGH)
        self.assertEqual(data.references, "EXTJS-1")
        self.assertEqual(data.estimate, "5m")
        self.assertEqual(data.preconditions, "pre")
        self.assertEqual(data.steps, "do it")
        self.assertEqual(data.expected, "works")

    def test_do_not_test_priority_maps_to_dont_test(self) -> None:
        data = self.mapper.to_test_case_data(_case(priority_id=5))
        self.assertEqual(data.priority, TestCasePriority.DONT_TEST)

    def test_smoke_type_matches_choice_with_ampersand(self) -> None:
        data = self.mapper.to_test_case_data(_case(type_id=11))
        self.assertEqual(data.type, TestCaseType.SMOKE_SANITY)

    def test_unknown_or_missing_ids_fall_back_to_defaults(self) -> None:
        data = self.mapper.to_test_case_data(_case(type_id=999, priority_id=None))
        self.assertEqual(data.type, TestCaseType.FUNCTIONAL)
        self.assertEqual(data.priority, TestCasePriority.MUST_TEST_HIGH)

    def test_steps_separated_flattens_and_sets_template(self) -> None:
        steps = (
            TestRailStep("Open app", "App opens"),
            TestRailStep("Click save", ""),
            TestRailStep("Reload", "Data persists"),
        )
        data = self.mapper.to_test_case_data(
            _case(
                template_id=3,
                steps="ignored",
                expected="ignored",
                steps_separated=steps,
            )
        )
        self.assertEqual(data.template, STEPS_TEMPLATE_NAME)
        self.assertEqual(data.steps, "1. Open app\n\n2. Click save\n\n3. Reload")
        self.assertEqual(data.expected, "1. App opens\n\n3. Data persists")

    def test_truncation(self) -> None:
        data = self.mapper.to_test_case_data(
            _case(title="t" * 600, refs="r" * 600, estimate="e" * 60)
        )
        self.assertEqual(len(data.title), 500)
        self.assertEqual(len(data.references), 500)
        self.assertEqual(len(data.estimate), 50)


class FlattenStepsTests(SimpleTestCase):
    def test_empty_returns_empty_pair(self) -> None:
        self.assertEqual(flatten_steps(()), ("", ""))
