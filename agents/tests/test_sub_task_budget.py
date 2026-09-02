from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from agents.services.sub_task_budget import (
    TimeoutBounds,
    build_timeout_bounds,
    resolve_timeout_seconds,
)

_BOUNDS = TimeoutBounds(default=180, minimum=60, maximum=900)


class ResolveTimeoutSecondsTests(SimpleTestCase):
    def test_integer_within_bounds_is_kept(self) -> None:
        self.assertEqual(resolve_timeout_seconds(300, _BOUNDS), 300)

    def test_float_is_truncated_to_whole_seconds(self) -> None:
        self.assertEqual(resolve_timeout_seconds(120.9, _BOUNDS), 120)

    def test_numeric_string_is_accepted(self) -> None:
        self.assertEqual(resolve_timeout_seconds("240", _BOUNDS), 240)

    def test_below_minimum_is_raised_to_minimum(self) -> None:
        self.assertEqual(resolve_timeout_seconds(5, _BOUNDS), 60)

    def test_above_maximum_is_lowered_to_maximum(self) -> None:
        self.assertEqual(resolve_timeout_seconds(3600, _BOUNDS), 900)

    def test_missing_value_falls_back_to_default(self) -> None:
        self.assertEqual(resolve_timeout_seconds(None, _BOUNDS), 180)

    def test_boolean_falls_back_to_default(self) -> None:
        self.assertEqual(resolve_timeout_seconds(True, _BOUNDS), 180)

    def test_garbage_string_falls_back_to_default(self) -> None:
        self.assertEqual(resolve_timeout_seconds("soon", _BOUNDS), 180)

    def test_default_is_itself_clamped(self) -> None:
        bounds = TimeoutBounds(default=10, minimum=60, maximum=900)
        self.assertEqual(resolve_timeout_seconds(None, bounds), 60)


class BuildTimeoutBoundsTests(SimpleTestCase):
    @override_settings(
        SUB_AGENT_TIMEOUT_SECONDS=200,
        SUB_AGENT_MIN_TIMEOUT_SECONDS=30,
        SUB_AGENT_MAX_TIMEOUT_SECONDS=600,
    )
    def test_reads_all_three_settings(self) -> None:
        self.assertEqual(
            build_timeout_bounds(),
            TimeoutBounds(default=200, minimum=30, maximum=600),
        )
