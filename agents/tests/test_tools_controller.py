from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from agents.services.tools_controller import click, key_press, type_text
from agents.types import LLMConfig
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


class ClickToolTests(SimpleTestCase):
    def test_reports_coordinates_when_controller_succeeds(self) -> None:
        with (
            patch(f"{_TOOLS}.find_element_coordinates", return_value=(1055, 697)),
            patch(f"{_TOOLS}.controller_click", return_value=_action(True)) as sent,
        ):
            result = click(7, description="the ^ button", vision_config=_LLM_CONFIG)

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "Clicked element at (1055, 697): the ^ button")
        sent.assert_called_once_with(7, 1055, 697)

    def test_controller_failure_becomes_tool_error_with_its_message(self) -> None:
        with (
            patch(f"{_TOOLS}.find_element_coordinates", return_value=(179, 392)),
            patch(
                f"{_TOOLS}.controller_click",
                return_value=_action(False, _BLOCKED_MESSAGE),
            ),
        ):
            result = click(7, description="Windows Explorer", vision_config=_LLM_CONFIG)

        self.assertTrue(result.is_error)
        self.assertIn(_BLOCKED_MESSAGE, result.content)
        self.assertNotIn("Clicked element", result.content)

    def test_controller_failure_without_message_still_errors(self) -> None:
        with (
            patch(f"{_TOOLS}.find_element_coordinates", return_value=(1, 2)),
            patch(f"{_TOOLS}.controller_click", return_value=_action(False)),
        ):
            result = click(7, description="anything", vision_config=_LLM_CONFIG)

        self.assertTrue(result.is_error)
        self.assertIn("Controller reported the action failed", result.content)


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
