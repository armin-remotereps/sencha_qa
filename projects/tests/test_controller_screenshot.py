from __future__ import annotations

from typing import Any
from unittest.mock import patch

from django.test import SimpleTestCase

from projects.services import (
    ControllerActionError,
    controller_browser_take_screenshot,
    controller_screenshot,
)

_DISPATCH = "projects.services._dispatch_controller_action"

_VALID_REPLY: dict[str, Any] = {
    "success": True,
    "image_base64": "aGVsbG8=",
    "width": 1440,
    "height": 900,
    "format": "png",
}


class ControllerScreenshotTests(SimpleTestCase):
    """A screenshot must never come back as an empty image.

    An image-less reply used to reach the vision model as a data URL with no
    payload, which the provider rejected with a 400 that named the LLM
    endpoint rather than the controller that actually failed.
    """

    def test_returns_the_image_when_the_controller_succeeds(self) -> None:
        with patch(_DISPATCH, return_value=dict(_VALID_REPLY)):
            result = controller_screenshot(1)

        self.assertEqual(result["image_base64"], "aGVsbG8=")
        self.assertEqual(result["width"], 1440)
        self.assertTrue(result["success"])

    def test_unsuccessful_reply_raises_with_the_controller_message(self) -> None:
        reply = {"success": False, "message": "Screenshot failed: display refused"}

        with patch(_DISPATCH, return_value=reply):
            with self.assertRaisesMessage(
                ControllerActionError, "Screenshot failed: display refused"
            ):
                controller_screenshot(1)

    def test_unsuccessful_reply_without_a_message_still_raises(self) -> None:
        with patch(_DISPATCH, return_value={"success": False}):
            with self.assertRaises(ControllerActionError):
                controller_screenshot(1)

    def test_empty_image_raises_rather_than_travelling_onward(self) -> None:
        reply = {"success": True, "image_base64": "", "width": 0, "height": 0}

        with patch(_DISPATCH, return_value=reply):
            with self.assertRaisesMessage(
                ControllerActionError, "empty image for take a screenshot"
            ):
                controller_screenshot(1)

    def test_browser_screenshot_applies_the_same_guard(self) -> None:
        reply = {"success": True, "image_base64": ""}

        with patch(_DISPATCH, return_value=reply):
            with self.assertRaisesMessage(
                ControllerActionError, "empty image for take a browser screenshot"
            ):
                controller_browser_take_screenshot(1)

    def test_browser_screenshot_returns_the_image_when_it_succeeds(self) -> None:
        with patch(_DISPATCH, return_value=dict(_VALID_REPLY)):
            result = controller_browser_take_screenshot(1)

        self.assertEqual(result["image_base64"], "aGVsbG8=")
