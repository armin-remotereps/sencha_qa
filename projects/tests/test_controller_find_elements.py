from __future__ import annotations

from typing import Any
from unittest.mock import patch

from django.test import SimpleTestCase

from projects.services import (
    ControllerActionError,
    _build_pixel_element,
    controller_find_elements,
)


class BuildPixelElementTests(SimpleTestCase):
    def test_builds_element_from_full_reply_data(self) -> None:
        data: dict[str, Any] = {
            "index": 3,
            "type": "icon",
            "content": "Submit",
            "bbox": {"x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4},
            "center_x": 2,
            "center_y": 3,
            "interactivity": True,
        }

        element = _build_pixel_element(data)

        self.assertEqual(element.index, 3)
        self.assertEqual(element.type, "icon")
        self.assertEqual(element.content, "Submit")
        self.assertEqual(element.bbox.x_min, 1)
        self.assertEqual(element.bbox.y_max, 4)
        self.assertEqual(element.center_x, 2)
        self.assertEqual(element.center_y, 3)
        self.assertTrue(element.interactivity)

    def test_defaults_missing_fields(self) -> None:
        element = _build_pixel_element({})

        self.assertEqual(element.index, 0)
        self.assertEqual(element.type, "unknown")
        self.assertEqual(element.content, "")
        self.assertEqual(element.bbox.x_min, 0)
        self.assertFalse(element.interactivity)


class ControllerFindElementsTests(SimpleTestCase):
    def test_builds_pixel_parse_result_on_success(self) -> None:
        reply: dict[str, Any] = {
            "success": True,
            "annotated_image_base64": "fake-base64",
            "image_width": 1920,
            "image_height": 1080,
            "elements": [
                {
                    "index": 0,
                    "type": "icon",
                    "content": "OK",
                    "bbox": {"x_min": 0, "y_min": 0, "x_max": 10, "y_max": 10},
                    "center_x": 5,
                    "center_y": 5,
                    "interactivity": True,
                }
            ],
        }

        with patch(
            "projects.services._dispatch_controller_action", return_value=reply
        ) as mock_dispatch:
            result = controller_find_elements(42, box_threshold=0.1, iou_threshold=0.6)

        mock_dispatch.assert_called_once_with(
            42,
            "controller.find_element",
            120.0,
            box_threshold=0.1,
            iou_threshold=0.6,
        )
        self.assertEqual(result.annotated_image, "fake-base64")
        self.assertEqual(result.image_width, 1920)
        self.assertEqual(len(result.elements), 1)
        self.assertEqual(result.elements[0].content, "OK")

    def test_raises_on_failed_reply(self) -> None:
        with patch(
            "projects.services._dispatch_controller_action",
            return_value={"success": False},
        ):
            with self.assertRaises(ControllerActionError):
                controller_find_elements(42)

    def test_raises_with_controllers_own_error_message_when_present(self) -> None:
        with patch(
            "projects.services._dispatch_controller_action",
            return_value={
                "success": False,
                "message": "OmniParser weights not found at '/bad/path'",
            },
        ):
            with self.assertRaisesMessage(
                ControllerActionError, "OmniParser weights not found at '/bad/path'"
            ):
                controller_find_elements(42)
