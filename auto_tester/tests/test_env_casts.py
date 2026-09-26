from __future__ import annotations

from django.test import SimpleTestCase

from auto_tester.env_casts import optional_float


class OptionalFloatTests(SimpleTestCase):
    def test_number_is_cast_to_float(self) -> None:
        self.assertEqual(optional_float("0.1"), 0.1)

    def test_zero_is_kept_not_treated_as_unset(self) -> None:
        self.assertEqual(optional_float("0"), 0.0)

    def test_empty_value_is_none(self) -> None:
        self.assertIsNone(optional_float(""))

    def test_whitespace_only_value_is_none(self) -> None:
        self.assertIsNone(optional_float("   "))

    def test_invalid_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            optional_float("warm")
