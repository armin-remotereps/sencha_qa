from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from projects.models import TestCasePriority, TestCaseType, TestRunTestCaseStatus
from projects.testrail_client import (
    TestRailCase,
    TestRailCaseType,
    TestRailPriority,
    TestRailStep,
)
from projects.testrail_mapping import (
    DEFAULT_TEMPLATE_NAME,
    RESULT_COMMENT_TEXT_MAX,
    STEPS_TEMPLATE_NAME,
    TESTRAIL_STATUS_FAILED,
    TESTRAIL_STATUS_PASSED,
    TestRailFieldMapper,
    build_result_comment,
    comment_hash,
    flatten_steps,
    format_testrail_elapsed,
    map_pivot_status_to_testrail,
    parse_testrail_case_id,
    testrail_outcome_label,
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


class MapPivotStatusToTestRailTests(SimpleTestCase):
    def test_success_maps_to_passed(self) -> None:
        self.assertEqual(
            map_pivot_status_to_testrail(TestRunTestCaseStatus.SUCCESS),
            TESTRAIL_STATUS_PASSED,
        )

    def test_failed_maps_to_failed(self) -> None:
        self.assertEqual(
            map_pivot_status_to_testrail(TestRunTestCaseStatus.FAILED),
            TESTRAIL_STATUS_FAILED,
        )

    def test_every_other_status_is_not_pushable(self) -> None:
        non_pushable_statuses = {
            TestRunTestCaseStatus.CREATED,
            TestRunTestCaseStatus.IN_PROGRESS,
            TestRunTestCaseStatus.CANCELLED,
        }
        self.assertEqual(
            non_pushable_statuses,
            set(TestRunTestCaseStatus.values)
            - {TestRunTestCaseStatus.SUCCESS, TestRunTestCaseStatus.FAILED},
        )
        for status in non_pushable_statuses:
            with self.subTest(status=status):
                self.assertIsNone(map_pivot_status_to_testrail(status))


class ParseTestrailCaseIdTests(SimpleTestCase):
    def test_bare_digits(self) -> None:
        self.assertEqual(parse_testrail_case_id("12"), 12)

    def test_uppercase_c_prefix(self) -> None:
        self.assertEqual(parse_testrail_case_id("C12"), 12)

    def test_lowercase_c_prefix(self) -> None:
        self.assertEqual(parse_testrail_case_id("c12"), 12)

    def test_whitespace_is_stripped(self) -> None:
        self.assertEqual(parse_testrail_case_id(" C12 "), 12)

    def test_non_numeric_is_none(self) -> None:
        self.assertIsNone(parse_testrail_case_id("abc"))

    def test_empty_string_is_none(self) -> None:
        self.assertIsNone(parse_testrail_case_id(""))

    def test_zero_is_none(self) -> None:
        self.assertIsNone(parse_testrail_case_id("0"))

    def test_bare_c_is_none(self) -> None:
        self.assertIsNone(parse_testrail_case_id("C"))


class FormatTestrailElapsedTests(SimpleTestCase):
    def _times(self, seconds: float) -> tuple[datetime, datetime]:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return start, start + timedelta(seconds=seconds)

    def test_missing_started_at_is_empty(self) -> None:
        _, finished = self._times(10)
        self.assertEqual(format_testrail_elapsed(None, finished), "")

    def test_missing_finished_at_is_empty(self) -> None:
        started, _ = self._times(10)
        self.assertEqual(format_testrail_elapsed(started, None), "")

    def test_both_missing_is_empty(self) -> None:
        self.assertEqual(format_testrail_elapsed(None, None), "")

    def test_under_one_second_is_empty(self) -> None:
        started, finished = self._times(0.4)
        self.assertEqual(format_testrail_elapsed(started, finished), "")

    def test_seconds_only(self) -> None:
        started, finished = self._times(45)
        self.assertEqual(format_testrail_elapsed(started, finished), "45s")

    def test_minutes_only(self) -> None:
        started, finished = self._times(120)
        self.assertEqual(format_testrail_elapsed(started, finished), "2m")

    def test_hours_and_seconds_omits_zero_minutes(self) -> None:
        started, finished = self._times(3603)
        self.assertEqual(format_testrail_elapsed(started, finished), "1h 3s")

    def test_hours_minutes_and_seconds(self) -> None:
        started, finished = self._times(3723)
        self.assertEqual(format_testrail_elapsed(started, finished), "1h 2m 3s")


class TestrailOutcomeLabelTests(SimpleTestCase):
    def test_passed(self) -> None:
        self.assertEqual(testrail_outcome_label(TESTRAIL_STATUS_PASSED), "Passed")

    def test_failed(self) -> None:
        self.assertEqual(testrail_outcome_label(TESTRAIL_STATUS_FAILED), "Failed")

    def test_unknown_status_falls_back_to_result(self) -> None:
        self.assertEqual(testrail_outcome_label(999), "Result")


class BuildResultCommentTests(SimpleTestCase):
    def test_with_result_text(self) -> None:
        comment = build_result_comment(
            outcome_label="Passed",
            run_name="Nightly Smoke",
            result_text="  Everything worked.  ",
            case_url="https://app.example.com/runs/1/cases/2/",
        )
        self.assertEqual(
            comment,
            "Passed by Punk Hazard in run “Nightly Smoke”.\n\n"
            "Everything worked.\n\n"
            "Details: https://app.example.com/runs/1/cases/2/",
        )

    def test_without_result_text_omits_paragraph(self) -> None:
        comment = build_result_comment(
            outcome_label="Failed",
            run_name="Nightly Smoke",
            result_text="   ",
            case_url="https://app.example.com/runs/1/cases/2/",
        )
        self.assertEqual(
            comment,
            "Failed by Punk Hazard in run “Nightly Smoke”.\n\n"
            "Details: https://app.example.com/runs/1/cases/2/",
        )

    def test_result_text_truncated_to_exactly_max_chars(self) -> None:
        comment = build_result_comment(
            outcome_label="Passed",
            run_name="Run",
            result_text="x" * (RESULT_COMMENT_TEXT_MAX + 500),
            case_url="https://example.com/",
        )
        paragraphs = comment.split("\n\n")
        self.assertEqual(len(paragraphs[1]), RESULT_COMMENT_TEXT_MAX)
        self.assertEqual(paragraphs[1], "x" * RESULT_COMMENT_TEXT_MAX)


class CommentHashTests(SimpleTestCase):
    def test_deterministic(self) -> None:
        self.assertEqual(comment_hash("hello"), comment_hash("hello"))

    def test_differs_for_different_input(self) -> None:
        self.assertNotEqual(comment_hash("hello"), comment_hash("world"))
