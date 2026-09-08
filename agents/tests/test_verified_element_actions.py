from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from agents.exceptions import (
    ActionVerificationError,
    ElementNotFoundError,
    RejectedElementChosenError,
)
from agents.services.verified_element_actions import (
    MAX_VERIFY_ATTEMPTS,
    VerificationBudget,
    confirm_action,
    format_verified_message,
    resolve_verified_element,
)
from agents.types import (
    AgentCancelledError,
    LLMConfig,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
    Verdict,
)
from projects.services import ScreenshotResult

_ACTIONS = "agents.services.verified_element_actions"

_LLM_CONFIG = LLMConfig(
    model="gpt-test",
    api_key="test-key",
    endpoint_url="https://example.test/v1",
    temperature=0.0,
    max_tokens=100,
)


def _element(index: int, content: str, center_x: int, center_y: int) -> PixelUIElement:
    return PixelUIElement(
        index=index,
        type="icon",
        content=content,
        bbox=PixelBBox(x_min=0, y_min=0, x_max=10, y_max=10),
        center_x=center_x,
        center_y=center_y,
        interactivity=True,
    )


_SAVE = _element(0, "Save", 100, 200)
_CANCEL = _element(1, "Cancel", 300, 200)
_PARSE = PixelParseResult(
    annotated_image="ANNOTATED",
    elements=(_SAVE, _CANCEL),
    image_width=1,
    image_height=1,
)


def _screenshot(image: str) -> ScreenshotResult:
    return ScreenshotResult(
        success=True, image_base64=image, width=1, height=1, format="png"
    )


def _accept() -> Verdict:
    return Verdict(accepted=True, reason="looks right")


def _reject(reason: str = "wrong element") -> Verdict:
    return Verdict(accepted=False, reason=reason)


class VerificationBudgetTests(SimpleTestCase):
    def test_consume_counts_attempts_until_the_cap(self) -> None:
        budget = VerificationBudget(max_attempts=2)

        budget.consume("Save")
        budget.consume("Save")

        with self.assertRaises(ActionVerificationError) as ctx:
            budget.consume("Save")
        self.assertIn("after 2 attempts", str(ctx.exception))

    def test_exhausted_message_lists_every_rejection(self) -> None:
        budget = VerificationBudget(max_attempts=2)
        budget.consume("Save")
        budget.reject(_CANCEL, "candidate", "that is Cancel")
        budget.consume("Save")
        budget.reject(_SAVE, "outcome", "nothing changed")

        with self.assertRaises(ActionVerificationError) as ctx:
            budget.consume("Save")

        message = str(ctx.exception)
        self.assertIn("attempt 1: candidate [1] 'Cancel' at (300, 200)", message)
        self.assertIn("that is Cancel", message)
        self.assertIn(
            "attempt 2: outcome rejected after acting on [0] 'Save' at (100, 200)",
            message,
        )
        self.assertIn("nothing changed", message)

    def test_rejections_keep_the_attempt_they_happened_on(self) -> None:
        budget = VerificationBudget()
        budget.consume("Save")
        budget.consume("Save")
        budget.reject(_SAVE, "outcome", "no change")
        budget.reject(_CANCEL, "outcome", "no change")

        self.assertEqual([r.attempt for r in budget.rejections], [2, 2])
        self.assertTrue(budget.history_lines()[1].startswith("attempt 2:"))

    def test_default_cap_is_ten(self) -> None:
        self.assertEqual(MAX_VERIFY_ATTEMPTS, 10)
        self.assertEqual(VerificationBudget().max_attempts, 10)

    def test_is_rejected_matches_nearby_positions_only(self) -> None:
        budget = VerificationBudget()
        budget.reject(_SAVE, "outcome", "no change")

        self.assertTrue(budget.is_rejected(_element(7, "Save", 108, 195)))
        self.assertFalse(budget.is_rejected(_element(7, "Save", 120, 200)))

    def test_consume_stops_when_deadline_is_too_close(self) -> None:
        budget = VerificationBudget(deadline=time.monotonic() + 1.0)

        with self.assertRaises(ActionVerificationError):
            budget.consume("Save")
        self.assertEqual(budget.used, 0)

    def test_consume_raises_when_cancelled(self) -> None:
        budget = VerificationBudget(cancellation_check=lambda: True)

        with self.assertRaises(AgentCancelledError):
            budget.consume("Save")


class ResolveVerifiedElementTests(SimpleTestCase):
    def test_accepts_first_candidate_after_one_attempt(self) -> None:
        budget = VerificationBudget()
        with (
            patch(f"{_ACTIONS}.parse_screen", return_value=_PARSE) as parse,
            patch(f"{_ACTIONS}.match_element", return_value=_SAVE),
            patch(f"{_ACTIONS}.verify_candidate", return_value=_accept()),
        ):
            element = resolve_verified_element(7, "Save", _LLM_CONFIG, budget)

        self.assertEqual(element, _SAVE)
        self.assertEqual(budget.used, 1)
        parse.assert_called_once()

    def test_rejected_candidate_is_excluded_from_the_next_match(self) -> None:
        budget = VerificationBudget()
        exclusion_checks: list[bool] = []

        def _match(
            parse_result: PixelParseResult,
            description: str,
            vision_config: LLMConfig,
            *,
            is_excluded: object,
        ) -> PixelUIElement:
            assert callable(is_excluded)
            exclusion_checks.append(bool(is_excluded(_CANCEL)))
            return _SAVE if exclusion_checks[-1] else _CANCEL

        with (
            patch(f"{_ACTIONS}.parse_screen", return_value=_PARSE) as parse,
            patch(f"{_ACTIONS}.match_element", side_effect=_match),
            patch(
                f"{_ACTIONS}.verify_candidate",
                side_effect=[_reject("that is Cancel"), _accept()],
            ),
        ):
            element = resolve_verified_element(7, "Save", _LLM_CONFIG, budget)

        self.assertEqual(element, _SAVE)
        self.assertEqual(exclusion_checks, [False, True])
        self.assertEqual(budget.used, 2)
        parse.assert_called_once()
        self.assertEqual(len(budget.rejections), 1)
        self.assertEqual(budget.rejections[0].stage, "candidate")

    def test_position_rejected_in_previous_parse_is_excluded(self) -> None:
        budget = VerificationBudget()
        budget.reject(_element(9, "Save", 102, 198), "outcome", "no change")

        def _match(
            parse_result: PixelParseResult,
            description: str,
            vision_config: LLMConfig,
            *,
            is_excluded: object,
        ) -> PixelUIElement:
            assert callable(is_excluded)
            self.assertTrue(is_excluded(_SAVE))
            self.assertFalse(is_excluded(_CANCEL))
            return _CANCEL

        with (
            patch(f"{_ACTIONS}.parse_screen", return_value=_PARSE),
            patch(f"{_ACTIONS}.match_element", side_effect=_match),
            patch(f"{_ACTIONS}.verify_candidate", return_value=_accept()),
        ):
            element = resolve_verified_element(7, "Save", _LLM_CONFIG, budget)

        self.assertEqual(element, _CANCEL)

    def test_gives_up_after_ten_rejections(self) -> None:
        budget = VerificationBudget()
        with (
            patch(f"{_ACTIONS}.parse_screen", return_value=_PARSE),
            patch(f"{_ACTIONS}.match_element", return_value=_CANCEL),
            patch(f"{_ACTIONS}.verify_candidate", return_value=_reject()) as verify,
        ):
            with self.assertRaises(ActionVerificationError) as ctx:
                resolve_verified_element(7, "Save", _LLM_CONFIG, budget)

        self.assertEqual(verify.call_count, 10)
        self.assertIn("after 10 attempts", str(ctx.exception))

    def test_matcher_repeating_a_ruled_out_element_counts_as_rejection(self) -> None:
        budget = VerificationBudget()
        with (
            patch(f"{_ACTIONS}.parse_screen", return_value=_PARSE),
            patch(
                f"{_ACTIONS}.match_element",
                side_effect=[
                    _CANCEL,
                    RejectedElementChosenError(_CANCEL, "Save"),
                    _SAVE,
                ],
            ),
            patch(
                f"{_ACTIONS}.verify_candidate",
                side_effect=[_reject("that is Cancel"), _accept()],
            ) as verify,
        ):
            element = resolve_verified_element(7, "Save", _LLM_CONFIG, budget)

        self.assertEqual(element, _SAVE)
        self.assertEqual(budget.used, 3)
        self.assertEqual(verify.call_count, 2)
        self.assertIn("already ruled out", budget.rejections[1].reason)

    def test_element_not_found_propagates(self) -> None:
        budget = VerificationBudget()
        with (
            patch(f"{_ACTIONS}.parse_screen", return_value=_PARSE),
            patch(
                f"{_ACTIONS}.match_element",
                side_effect=ElementNotFoundError("nothing left"),
            ),
        ):
            with self.assertRaises(ElementNotFoundError):
                resolve_verified_element(7, "Save", _LLM_CONFIG, budget)


class ConfirmActionTests(SimpleTestCase):
    def test_accepted_outcome_publishes_after_screenshot(self) -> None:
        budget = VerificationBudget()
        on_screenshot = MagicMock()
        with (
            patch(f"{_ACTIONS}.time.sleep"),
            patch(
                f"{_ACTIONS}.controller_screenshot", return_value=_screenshot("AFTER")
            ),
            patch(
                f"{_ACTIONS}.verify_action_outcome", return_value=_accept()
            ) as verify,
        ):
            verdict = confirm_action(
                7,
                _LLM_CONFIG,
                budget,
                acted_on=(_SAVE,),
                before_image_base64="BEFORE",
                action_summary="clicked 'Save' at (100, 200)",
                expected_result="the file is saved",
                tool_name="click",
                on_screenshot=on_screenshot,
            )

        self.assertTrue(verdict.accepted)
        on_screenshot.assert_called_once_with("AFTER", "click")
        verify.assert_called_once_with(
            _LLM_CONFIG,
            "BEFORE",
            "AFTER",
            "clicked 'Save' at (100, 200)",
            "the file is saved",
        )
        self.assertEqual(budget.rejections, [])

    def test_rejected_outcome_is_recorded_on_the_budget(self) -> None:
        budget = VerificationBudget()
        with (
            patch(f"{_ACTIONS}.time.sleep"),
            patch(
                f"{_ACTIONS}.controller_screenshot", return_value=_screenshot("AFTER")
            ),
            patch(
                f"{_ACTIONS}.verify_action_outcome",
                return_value=_reject("nothing changed"),
            ),
        ):
            verdict = confirm_action(
                7,
                _LLM_CONFIG,
                budget,
                acted_on=(_SAVE, _CANCEL),
                before_image_base64="BEFORE",
                action_summary="dragged 'Save' onto 'Cancel'",
                expected_result="the file is saved",
                tool_name="drag",
            )

        self.assertFalse(verdict.accepted)
        self.assertEqual([r.stage for r in budget.rejections], ["outcome", "outcome"])
        self.assertTrue(budget.is_rejected(_SAVE))
        self.assertTrue(budget.is_rejected(_CANCEL))


class FormatVerifiedMessageTests(SimpleTestCase):
    def test_first_attempt_has_no_skipped_section(self) -> None:
        budget = VerificationBudget()
        budget.consume("Save")

        message = format_verified_message("Clicked element at (1, 2): Save", budget)

        self.assertEqual(
            message, "Clicked element at (1, 2): Save (verified on attempt 1)"
        )

    def test_later_attempts_list_skipped_candidates(self) -> None:
        budget = VerificationBudget()
        budget.consume("Save")
        budget.reject(_CANCEL, "candidate", "that is Cancel")
        budget.consume("Save")

        message = format_verified_message("Clicked element at (1, 2): Save", budget)

        self.assertIn("(verified on attempt 2)", message)
        self.assertIn("Skipped candidates:", message)
        self.assertIn("candidate [1] 'Cancel'", message)
