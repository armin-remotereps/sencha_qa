from __future__ import annotations

import base64
import sys
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase, override_settings

from omniparser_wrapper.services.parser import (
    OmniParserService,
    RawElementDict,
    _build_draw_config,
    _build_element,
    _resolve_thresholds,
    _to_pixel_element,
)
from omniparser_wrapper.types import BBox, ParseResult, PixelParseResult, UIElement


class BuildElementTest(TestCase):
    def test_basic_element(self) -> None:
        raw: RawElementDict = {
            "type": "text",
            "content": "Submit",
            "bbox": [0.1, 0.2, 0.3, 0.4],
            "interactivity": True,
        }
        element = _build_element(0, raw)
        assert element.index == 0
        assert element.type == "text"
        assert element.content == "Submit"
        assert element.bbox == BBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4)
        assert element.center_x == pytest.approx(0.2)
        assert element.center_y == pytest.approx(0.3)
        assert element.interactivity is True

    def test_missing_fields_use_defaults(self) -> None:
        raw: RawElementDict = {"bbox": [0.0, 0.0, 1.0, 1.0]}
        element = _build_element(3, raw)
        assert element.type == "unknown"
        assert element.content == ""
        assert element.interactivity is False

    def test_center_calculation(self) -> None:
        raw: RawElementDict = {
            "type": "icon",
            "content": "X",
            "bbox": [0.2, 0.4, 0.6, 0.8],
            "interactivity": False,
        }
        element = _build_element(1, raw)
        assert element.center_x == pytest.approx(0.4)
        assert element.center_y == pytest.approx(0.6)


class ToPixelElementTest(TestCase):
    def test_conversion(self) -> None:
        element = UIElement(
            index=0,
            type="text",
            content="OK",
            bbox=BBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4),
            center_x=0.2,
            center_y=0.3,
            interactivity=False,
        )
        pixel = _to_pixel_element(element, width=1920, height=1080)
        assert pixel.bbox.x_min == 192
        assert pixel.bbox.y_min == 216
        assert pixel.bbox.x_max == 576
        assert pixel.bbox.y_max == 432
        assert pixel.center_x == 384
        assert pixel.center_y == 324

    def test_rounding(self) -> None:
        element = UIElement(
            index=0,
            type="text",
            content="Hi",
            bbox=BBox(x_min=0.333, y_min=0.666, x_max=0.999, y_max=0.111),
            center_x=0.666,
            center_y=0.3885,
            interactivity=True,
        )
        pixel = _to_pixel_element(element, width=100, height=100)
        assert pixel.bbox.x_min == 33
        assert pixel.bbox.y_min == 67
        assert pixel.bbox.x_max == 100
        assert pixel.bbox.y_max == 11
        assert pixel.center_x == 67
        assert pixel.center_y == 39


class BuildDrawConfigTest(TestCase):
    def test_draw_config_keys(self) -> None:
        config = _build_draw_config((1920, 1080))
        assert "text_scale" in config
        assert "text_thickness" in config
        assert "text_padding" in config
        assert "thickness" in config

    def test_draw_config_scales_with_image_size(self) -> None:
        small = _build_draw_config((640, 480))
        large = _build_draw_config((3840, 2160))
        assert small["text_scale"] < large["text_scale"]


class ResolveThresholdsTest(TestCase):
    @override_settings(OMNIPARSER_BOX_THRESHOLD=0.05, OMNIPARSER_IOU_THRESHOLD=0.7)
    def test_uses_settings_defaults(self) -> None:
        box, iou = _resolve_thresholds(None, None)
        assert box == 0.05
        assert iou == 0.7

    @override_settings(OMNIPARSER_BOX_THRESHOLD=0.05, OMNIPARSER_IOU_THRESHOLD=0.7)
    def test_overrides_with_provided_values(self) -> None:
        box, iou = _resolve_thresholds(0.1, 0.5)
        assert box == 0.1
        assert iou == 0.5


class OmniParserServiceSingletonTest(TestCase):
    def test_singleton(self) -> None:
        OmniParserService._instance = None
        a = OmniParserService()
        b = OmniParserService()
        assert a is b
        OmniParserService._instance = None

    def test_models_not_loaded_initially(self) -> None:
        OmniParserService._instance = None
        OmniParserService._parser = None
        service = OmniParserService()
        assert service.models_loaded is False
        OmniParserService._instance = None
        OmniParserService._parser = None


class OmniParserServiceParseTest(TestCase):
    def setUp(self) -> None:
        OmniParserService._instance = None
        OmniParserService._parser = None

    def tearDown(self) -> None:
        OmniParserService._instance = None
        OmniParserService._parser = None

    @override_settings(
        OMNIPARSER_WEIGHTS_DIR="/fake/weights",
        OMNIPARSER_BOX_THRESHOLD=0.05,
        OMNIPARSER_IOU_THRESHOLD=0.7,
        OMNIPARSER_CAPTION_BATCH_SIZE=64,
    )
    @patch("omniparser_wrapper.services.parser._ensure_omniparser_on_path")
    def test_parse_returns_parse_result(self, mock_path: MagicMock) -> None:
        service = OmniParserService()

        mock_parser = MagicMock()
        mock_parser.som_model = MagicMock()
        mock_parser.caption_model_processor = MagicMock()
        service._parser = mock_parser

        fake_parsed = [
            {
                "type": "text",
                "content": "OK",
                "bbox": [0.1, 0.2, 0.3, 0.4],
                "interactivity": False,
            },
        ]

        mock_image = MagicMock()
        mock_image.size = (1920, 1080)

        with (
            patch(
                "omniparser_wrapper.services.parser._decode_image",
                return_value=(mock_image, 1920, 1080),
            ),
            patch(
                "omniparser_wrapper.services.parser._run_ocr",
                return_value=("text", [[0.1, 0.2, 0.3, 0.4]]),
            ),
            patch(
                "omniparser_wrapper.services.parser._run_som_labeling",
                return_value=("annotated_b64", fake_parsed),
            ),
        ):
            fake_b64 = base64.b64encode(b"fake_image_data").decode()
            result = service.parse(fake_b64)

        assert isinstance(result, ParseResult)
        assert result.annotated_image == "annotated_b64"
        assert len(result.elements) == 1
        assert result.elements[0].content == "OK"
        assert result.image_width == 1920
        assert result.image_height == 1080

    @override_settings(
        OMNIPARSER_WEIGHTS_DIR="/fake/weights",
        OMNIPARSER_BOX_THRESHOLD=0.05,
        OMNIPARSER_IOU_THRESHOLD=0.7,
        OMNIPARSER_CAPTION_BATCH_SIZE=64,
    )
    @patch("omniparser_wrapper.services.parser._ensure_omniparser_on_path")
    def test_parse_pixels_returns_pixel_result(self, mock_path: MagicMock) -> None:
        service = OmniParserService()

        mock_parser = MagicMock()
        mock_parser.som_model = MagicMock()
        mock_parser.caption_model_processor = MagicMock()
        service._parser = mock_parser

        fake_parsed = [
            {
                "type": "icon",
                "content": "Close",
                "bbox": [0.5, 0.5, 0.6, 0.6],
                "interactivity": True,
            },
        ]

        mock_image = MagicMock()
        mock_image.size = (1000, 1000)

        with (
            patch(
                "omniparser_wrapper.services.parser._decode_image",
                return_value=(mock_image, 1000, 1000),
            ),
            patch(
                "omniparser_wrapper.services.parser._run_ocr",
                return_value=("text", []),
            ),
            patch(
                "omniparser_wrapper.services.parser._run_som_labeling",
                return_value=("annotated_b64", fake_parsed),
            ),
        ):
            fake_b64 = base64.b64encode(b"fake_image_data").decode()
            result = service.parse_pixels(fake_b64)

        assert isinstance(result, PixelParseResult)
        assert result.elements[0].bbox.x_min == 500
        assert result.elements[0].bbox.y_min == 500
        assert result.elements[0].center_x == 550
        assert result.elements[0].center_y == 550
