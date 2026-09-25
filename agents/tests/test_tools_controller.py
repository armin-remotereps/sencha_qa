from __future__ import annotations

from collections.abc import Callable
from unittest.mock import patch

from django.test import SimpleTestCase

from agents.exceptions import ElementNotFoundError
from agents.services.tools_controller import click, drag, hover, key_press, type_text
from agents.services.verified_element_actions import VerificationBudget
from agents.types import (
    AgentCancelledError,
    LLMConfig,
    PixelBBox,
    PixelUIElement,
    Verdict,
)
from projects.services import ActionResult

_TOOLS = "agents.services.tools_controller"

_LLM_CONFIG = LLMConfig(
    model="gpt-test",
    api_key="test-key",
    endpoint_url="https://example.test/v1",
    temperature=0.0,
    max_tokens=100,
)

_BLOCKED_MESSAGE = (
    "Windows will discard synthesized input: the foreground window "
    "'Task Manager' (pid 4242) runs at high (elevated) integrity"
)


def _action(success: bool, message: str = "") -> ActionResult:
    return ActionResult(success=success, message=message, duration_ms=1.0)


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


_CARET = _element(4, "^", 1055, 697)
_TRASH = _element(9, "Trash", 40, 900)


def _consume(budget: VerificationBudget, description: str) -> None:
    budget.consume(description)


def _resolving(*elements: PixelUIElement) -> Callable[..., PixelUIElement]:
    queue = list(elements)

    def _resolve(
        project_id: int,
        description: str,
        vision_config: LLMConfig,
        budget: VerificationBudget,
        *,
        on_screenshot: object = None,
    ) -> PixelUIElement:
        _consume(budget, description)
        return queue.pop(0)

    return _resolve


def _confirming(*verdicts: Verdict) -> Callable[..., Verdict]:
    queue = list(verdicts)

    def _confirm(
        project_id: int,
        vision_config: LLMConfig,
        budget: VerificationBudget,
        *,
        acted_on: tuple[PixelUIElement, ...],
        before_image_base64: str,
        action_summary: str,
        expected_result: str,
        tool_name: str,
        on_screenshot: object = None,
    ) -> Verdict:
        verdict = queue.pop(0)
        if not verdict.accepted:
            for element in acted_on:
                budget.reject(element, "outcome", verdict.reason)
        return verdict

    return _confirm


_YES = Verdict(accepted=True, reason="ok")
_NO = Verdict(accepted=False, reason="nothing changed")


class ClickToolTests(SimpleTestCase):
    def test_reports_coordinates_when_click_is_verified(self) -> None:
        with (
            patch(f"{_TOOLS}.resolve_verified_element", side_effect=_resolving(_CARET)),
            patch(f"{_TOOLS}.take_before_screenshot", return_value="BEFORE"),
            patch(f"{_TOOLS}.controller_click", return_value=_action(True)) as sent,
            patch(f"{_TOOLS}.confirm_action", side_effect=_confirming(_YES)) as confirm,
        ):
            result = click(7, description="the ^ button", vision_config=_LLM_CONFIG)

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.content,
            "Clicked element at (1055, 697): the ^ button (verified on attempt 1)",
        )
        sent.assert_called_once_with(7, 1055, 697)
        kwargs = confirm.call_args.kwargs
        self.assertEqual(kwargs["before_image_base64"], "BEFORE")
        self.assertEqual(kwargs["tool_name"], "click")
        self.assertIn("the ^ button", kwargs["expected_result"])

    def test_passes_expected_result_to_the_outcome_check(self) -> None:
        with (
            patch(f"{_TOOLS}.resolve_verified_element", side_effect=_resolving(_CARET)),
            patch(f"{_TOOLS}.take_before_screenshot", return_value="BEFORE"),
            patch(f"{_TOOLS}.controller_click", return_value=_action(True)),
            patch(f"{_TOOLS}.confirm_action", side_effect=_confirming(_YES)) as confirm,
        ):
            click(
                7,
                description="the ^ button",
                expected_result="the tray expands",
                vision_config=_LLM_CONFIG,
            )

        self.assertEqual(
            confirm.call_args.kwargs["expected_result"], "the tray expands"
        )

    def test_retries_with_another_candidate_when_outcome_is_rejected(self) -> None:
        with (
            patch(
                f"{_TOOLS}.resolve_verified_element",
                side_effect=_resolving(_TRASH, _CARET),
            ),
            patch(f"{_TOOLS}.take_before_screenshot", return_value="BEFORE"),
            patch(f"{_TOOLS}.controller_click", return_value=_action(True)) as sent,
            patch(f"{_TOOLS}.confirm_action", side_effect=_confirming(_NO, _YES)),
        ):
            result = click(7, description="the ^ button", vision_config=_LLM_CONFIG)

        self.assertFalse(result.is_error)
        self.assertEqual(sent.call_count, 2)
        self.assertIn("(verified on attempt 2)", result.content)
        self.assertIn("Skipped candidates:", result.content)
        self.assertIn("outcome rejected", result.content)

    def test_gives_up_with_history_after_the_attempt_cap(self) -> None:
        with (
            patch(
                f"{_TOOLS}.resolve_verified_element",
                side_effect=_resolving(*([_TRASH] * 10)),
            ),
            patch(f"{_TOOLS}.take_before_screenshot", return_value="BEFORE"),
            patch(f"{_TOOLS}.controller_click", return_value=_action(True)) as sent,
            patch(f"{_TOOLS}.confirm_action", side_effect=_confirming(*([_NO] * 10))),
        ):
            result = click(7, description="the ^ button", vision_config=_LLM_CONFIG)

        self.assertTrue(result.is_error)
        self.assertEqual(sent.call_count, 10)
        self.assertIn(
            "could not verify 'the ^ button' after 10 attempts", result.content
        )
        self.assertIn("attempt 10: outcome rejected", result.content)

    def test_controller_failure_becomes_tool_error_with_its_message(self) -> None:
        with (
            patch(f"{_TOOLS}.resolve_verified_element", side_effect=_resolving(_CARET)),
            patch(f"{_TOOLS}.take_before_screenshot", return_value="BEFORE"),
            patch(
                f"{_TOOLS}.controller_click",
                return_value=_action(False, _BLOCKED_MESSAGE),
            ),
            patch(f"{_TOOLS}.confirm_action") as confirm,
        ):
            result = click(7, description="Windows Explorer", vision_config=_LLM_CONFIG)

        self.assertTrue(result.is_error)
        self.assertIn(_BLOCKED_MESSAGE, result.content)
        self.assertNotIn("Clicked element", result.content)
        confirm.assert_not_called()

    def test_controller_failure_without_message_still_errors(self) -> None:
        with (
            patch(f"{_TOOLS}.resolve_verified_element", side_effect=_resolving(_CARET)),
            patch(f"{_TOOLS}.take_before_screenshot", return_value="BEFORE"),
            patch(f"{_TOOLS}.controller_click", return_value=_action(False)),
        ):
            result = click(7, description="anything", vision_config=_LLM_CONFIG)

        self.assertTrue(result.is_error)
        self.assertIn("Controller reported the action failed", result.content)

    def test_missing_element_is_reported_without_clicking(self) -> None:
        not_found = ElementNotFoundError(
            "element 'the ^ button' was not found on screen: 3 candidates were "
            "checked and none matched:"
        )
        with (
            patch(f"{_TOOLS}.resolve_verified_element", side_effect=not_found),
            patch(f"{_TOOLS}.controller_click") as sent,
        ):
            result = click(7, description="the ^ button", vision_config=_LLM_CONFIG)

        self.assertTrue(result.is_error)
        self.assertIn(
            "click error: element 'the ^ button' was not found on screen",
            result.content,
        )
        sent.assert_not_called()

    def test_cancellation_propagates_out_of_the_tool(self) -> None:
        with self.assertRaises(AgentCancelledError):
            click(
                7,
                description="anything",
                vision_config=_LLM_CONFIG,
                cancellation_check=lambda: True,
            )


class HoverToolTests(SimpleTestCase):
    def test_skips_outcome_check_without_expected_result(self) -> None:
        with (
            patch(f"{_TOOLS}.resolve_verified_element", side_effect=_resolving(_CARET)),
            patch(f"{_TOOLS}.take_before_screenshot") as before,
            patch(f"{_TOOLS}.controller_hover", return_value=_action(True)) as sent,
            patch(f"{_TOOLS}.confirm_action") as confirm,
        ):
            result = hover(7, description="the ^ button", vision_config=_LLM_CONFIG)

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.content,
            "Hovered over element at (1055, 697): the ^ button (verified on attempt 1)",
        )
        sent.assert_called_once_with(7, 1055, 697)
        before.assert_not_called()
        confirm.assert_not_called()

    def test_confirms_outcome_when_expected_result_is_given(self) -> None:
        with (
            patch(
                f"{_TOOLS}.resolve_verified_element",
                side_effect=_resolving(_TRASH, _CARET),
            ),
            patch(f"{_TOOLS}.take_before_screenshot", return_value="BEFORE"),
            patch(f"{_TOOLS}.controller_hover", return_value=_action(True)) as sent,
            patch(
                f"{_TOOLS}.confirm_action", side_effect=_confirming(_NO, _YES)
            ) as confirm,
        ):
            result = hover(
                7,
                description="the ^ button",
                expected_result="a tooltip appears",
                vision_config=_LLM_CONFIG,
            )

        self.assertFalse(result.is_error)
        self.assertEqual(sent.call_count, 2)
        self.assertEqual(confirm.call_args.kwargs["tool_name"], "hover")
        self.assertIn("(verified on attempt 2)", result.content)


class DragToolTests(SimpleTestCase):
    def test_resolves_both_ends_and_skips_outcome_check_by_default(self) -> None:
        with (
            patch(
                f"{_TOOLS}.resolve_verified_element",
                side_effect=_resolving(_CARET, _TRASH),
            ) as resolve,
            patch(f"{_TOOLS}.controller_drag", return_value=_action(True)) as sent,
            patch(f"{_TOOLS}.confirm_action") as confirm,
        ):
            result = drag(
                7,
                start_description="the file",
                end_description="the trash",
                vision_config=_LLM_CONFIG,
            )

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.content,
            "Dragged from (1055, 697) to (40, 900) (verified on attempt 2)",
        )
        sent.assert_called_once_with(7, 1055, 697, 40, 900)
        self.assertEqual(
            [c.args[1] for c in resolve.call_args_list], ["the file", "the trash"]
        )
        confirm.assert_not_called()

    def test_re_resolves_both_ends_when_outcome_is_rejected(self) -> None:
        with (
            patch(
                f"{_TOOLS}.resolve_verified_element",
                side_effect=_resolving(_CARET, _TRASH, _CARET, _TRASH),
            ) as resolve,
            patch(f"{_TOOLS}.take_before_screenshot", return_value="BEFORE"),
            patch(f"{_TOOLS}.controller_drag", return_value=_action(True)) as sent,
            patch(f"{_TOOLS}.confirm_action", side_effect=_confirming(_NO, _YES)),
        ):
            result = drag(
                7,
                start_description="the file",
                end_description="the trash",
                expected_result="the file disappears",
                vision_config=_LLM_CONFIG,
            )

        self.assertFalse(result.is_error)
        self.assertEqual(resolve.call_count, 4)
        self.assertEqual(sent.call_count, 2)
        self.assertIn("(verified on attempt 4)", result.content)
        self.assertIn("acting on [4] '^'", result.content)
        self.assertIn("acting on [9] 'Trash'", result.content)


class KeyboardToolTests(SimpleTestCase):
    def test_type_text_surfaces_controller_failure(self) -> None:
        with patch(
            f"{_TOOLS}.controller_type_text",
            return_value=_action(False, _BLOCKED_MESSAGE),
        ):
            result = type_text(7, text="explorer")

        self.assertTrue(result.is_error)
        self.assertIn(_BLOCKED_MESSAGE, result.content)

    def test_key_press_surfaces_controller_failure(self) -> None:
        with patch(
            f"{_TOOLS}.controller_key_press",
            return_value=_action(False, _BLOCKED_MESSAGE),
        ):
            result = key_press(7, keys="enter")

        self.assertTrue(result.is_error)
        self.assertIn(_BLOCKED_MESSAGE, result.content)

    def test_key_press_success_is_unchanged(self) -> None:
        with patch(f"{_TOOLS}.controller_key_press", return_value=_action(True)):
            result = key_press(7, keys="enter")

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "Pressed keys: enter")
