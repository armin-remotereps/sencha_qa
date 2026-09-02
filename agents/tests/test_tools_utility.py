from __future__ import annotations

import math
from unittest.mock import patch

from django.test import SimpleTestCase

from agents.services import tools_utility
from agents.services.prompt_parts import (
    build_tool_guidelines,
    build_utility_tool_examples,
)
from agents.services.tool_definitions import get_all_tool_definitions
from agents.services.tool_registry import dispatch_tool_call
from agents.types import (
    AgentCancelledError,
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    ToolResult,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


def _wait_definition() -> ToolDefinition:
    matches = [d for d in get_all_tool_definitions() if d.name == "wait"]
    assert len(matches) == 1
    return matches[0]


class WaitToolDefinitionTests(SimpleTestCase):
    def test_wait_appears_exactly_once_in_all_definitions(self) -> None:
        names = [d.name for d in get_all_tool_definitions()]
        self.assertEqual(names.count("wait"), 1)

    def test_wait_is_a_utility_tool(self) -> None:
        self.assertEqual(_wait_definition().category, ToolCategory.UTILITY)

    def test_seconds_parameter_is_required_number(self) -> None:
        params = _wait_definition().parameters
        self.assertEqual(len(params), 1)
        seconds = params[0]
        self.assertEqual(seconds.name, "seconds")
        self.assertEqual(seconds.type, "number")
        self.assertTrue(seconds.required)


class _FakeClockTestCase(SimpleTestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock()
        monotonic_patch = patch(
            "agents.services.tools_utility.time.monotonic", self.clock.monotonic
        )
        sleep_patch = patch(
            "agents.services.tools_utility.time.sleep", self.clock.sleep
        )
        monotonic_patch.start()
        sleep_patch.start()
        self.addCleanup(monotonic_patch.stop)
        self.addCleanup(sleep_patch.stop)


class WaitOperationTests(_FakeClockTestCase):
    def test_integer_duration_succeeds(self) -> None:
        result = tools_utility.wait(10)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "Waited 10 seconds.")
        self.assertAlmostEqual(sum(self.clock.sleep_calls), 10.0)

    def test_fractional_duration_succeeds(self) -> None:
        result = tools_utility.wait(0.5)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "Waited 0.5 seconds.")
        self.assertAlmostEqual(sum(self.clock.sleep_calls), 0.5)

    def test_whole_float_is_reported_without_decimal(self) -> None:
        result = tools_utility.wait(3.0)

        self.assertEqual(result.content, "Waited 3 seconds.")

    def test_sleeps_in_short_chunks(self) -> None:
        tools_utility.wait(2)

        self.assertGreater(len(self.clock.sleep_calls), 1)
        self.assertTrue(all(chunk <= 0.25 for chunk in self.clock.sleep_calls))

    def test_never_sleeps_past_the_deadline(self) -> None:
        tools_utility.wait(0.6)

        self.assertAlmostEqual(self.clock.now, 1000.6)

    def test_maximum_duration_is_accepted(self) -> None:
        result = tools_utility.wait(300)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "Waited 300 seconds.")

    def test_cancellation_interrupts_wait(self) -> None:
        checks: list[bool] = [False, False, True]

        def cancellation_check() -> bool:
            return checks.pop(0) if checks else True

        with self.assertRaises(AgentCancelledError):
            tools_utility.wait(60, cancellation_check=cancellation_check)

        self.assertLess(self.clock.now, 1000.0 + 60)

    def test_wait_without_cancellation_check_completes(self) -> None:
        result = tools_utility.wait(1, cancellation_check=None)

        self.assertFalse(result.is_error)


class WaitBudgetTests(_FakeClockTestCase):
    def test_wait_within_budget_is_unchanged(self) -> None:
        result = tools_utility.wait(10, deadline=self.clock.now + 100)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "Waited 10 seconds.")
        self.assertAlmostEqual(self.clock.now, 1010.0)

    def test_wait_exceeding_budget_is_shortened_with_margin(self) -> None:
        result = tools_utility.wait(300, deadline=self.clock.now + 100)

        self.assertFalse(result.is_error)
        self.assertIn("Waited 85 of the 300 seconds requested", result.content)
        self.assertIn("report the result now", result.content)
        self.assertAlmostEqual(self.clock.now, 1085.0)

    def test_exhausted_budget_does_not_sleep(self) -> None:
        result = tools_utility.wait(30, deadline=self.clock.now + 15.5)

        self.assertTrue(result.is_error)
        self.assertIn("time budget is exhausted", result.content)
        self.assertEqual(self.clock.sleep_calls, [])

    def test_past_deadline_does_not_sleep(self) -> None:
        result = tools_utility.wait(30, deadline=self.clock.now - 5)

        self.assertTrue(result.is_error)
        self.assertEqual(self.clock.sleep_calls, [])

    def test_invalid_duration_is_rejected_before_budget_check(self) -> None:
        result = tools_utility.wait(0, deadline=self.clock.now - 5)

        self.assertTrue(result.is_error)
        self.assertIn("Invalid seconds value", result.content)


class WaitValidationTests(SimpleTestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock()
        sleep_patch = patch(
            "agents.services.tools_utility.time.sleep", self.clock.sleep
        )
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _assert_rejected(self, value: object) -> None:
        result = tools_utility.wait(value)

        self.assertTrue(result.is_error, msg=f"{value!r} should be rejected")
        self.assertEqual(self.clock.sleep_calls, [], msg=f"{value!r} slept")

    def test_missing_value_is_rejected(self) -> None:
        self._assert_rejected(None)

    def test_string_is_rejected(self) -> None:
        self._assert_rejected("10")

    def test_boolean_is_rejected(self) -> None:
        self._assert_rejected(True)

    def test_zero_is_rejected(self) -> None:
        self._assert_rejected(0)

    def test_negative_is_rejected(self) -> None:
        self._assert_rejected(-1)

    def test_above_maximum_is_rejected(self) -> None:
        self._assert_rejected(300.01)

    def test_nan_is_rejected(self) -> None:
        self._assert_rejected(math.nan)

    def test_infinity_is_rejected(self) -> None:
        self._assert_rejected(math.inf)

    def test_negative_infinity_is_rejected(self) -> None:
        self._assert_rejected(-math.inf)

    def test_error_message_mentions_valid_range(self) -> None:
        result = tools_utility.wait(500)

        self.assertIn("300", result.content)


class WaitDispatchTests(SimpleTestCase):
    def test_dispatch_reaches_utility_handler_with_seconds(self) -> None:
        context = ToolContext(project_id=1)
        tool_call = ToolCall(
            tool_call_id="call-abc", tool_name="wait", arguments={"seconds": 2}
        )

        with patch("agents.services.tools_utility.wait") as mocked_wait:
            mocked_wait.return_value = ToolResult(
                tool_call_id="", content="Waited 2 seconds.", is_error=False
            )
            dispatch_tool_call(tool_call, context)

        mocked_wait.assert_called_once_with(2, cancellation_check=None, deadline=None)

    def test_dispatch_forwards_deadline_from_context(self) -> None:
        context = ToolContext(project_id=1, deadline=1234.5)
        tool_call = ToolCall(
            tool_call_id="call-abc", tool_name="wait", arguments={"seconds": 2}
        )

        with patch("agents.services.tools_utility.wait") as mocked_wait:
            mocked_wait.return_value = ToolResult(
                tool_call_id="", content="Waited 2 seconds.", is_error=False
            )
            dispatch_tool_call(tool_call, context)

        mocked_wait.assert_called_once_with(2, cancellation_check=None, deadline=1234.5)

    def test_dispatch_forwards_cancellation_check_from_context(self) -> None:
        def cancellation_check() -> bool:
            return False

        context = ToolContext(project_id=1, cancellation_check=cancellation_check)
        tool_call = ToolCall(
            tool_call_id="call-abc", tool_name="wait", arguments={"seconds": 2}
        )

        with patch("agents.services.tools_utility.wait") as mocked_wait:
            mocked_wait.return_value = ToolResult(
                tool_call_id="", content="Waited 2 seconds.", is_error=False
            )
            dispatch_tool_call(tool_call, context)

        mocked_wait.assert_called_once_with(
            2, cancellation_check=cancellation_check, deadline=None
        )

    def test_dispatch_preserves_original_tool_call_id(self) -> None:
        context = ToolContext(project_id=1)
        tool_call = ToolCall(
            tool_call_id="call-xyz", tool_name="wait", arguments={"seconds": 1}
        )

        clock = _FakeClock()
        with (
            patch("agents.services.tools_utility.time.monotonic", clock.monotonic),
            patch("agents.services.tools_utility.time.sleep", clock.sleep),
        ):
            result = dispatch_tool_call(tool_call, context)

        self.assertEqual(result.tool_call_id, "call-xyz")

    def test_dispatch_with_missing_seconds_returns_error(self) -> None:
        context = ToolContext(project_id=1)
        tool_call = ToolCall(tool_call_id="call-1", tool_name="wait", arguments={})

        with patch("agents.services.tools_utility.time.sleep") as mocked_sleep:
            result = dispatch_tool_call(tool_call, context)

        self.assertTrue(result.is_error)
        self.assertEqual(result.tool_call_id, "call-1")
        mocked_sleep.assert_not_called()


class WaitPromptGuidanceTests(SimpleTestCase):
    def test_guidance_mentions_wait_example(self) -> None:
        self.assertIn("wait(seconds=5)", build_tool_guidelines())

    def test_referenced_tool_names_exist(self) -> None:
        defined = {d.name for d in get_all_tool_definitions()}
        for name in ("wait", "wait_for_command", "browser_list_downloads"):
            self.assertIn(name, defined)
            self.assertIn(name, build_utility_tool_examples())
