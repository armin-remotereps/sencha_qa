from __future__ import annotations

import pytest

from omniparser_wrapper.types import (
    BBox,
    ParseResult,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
    UIElement,
)


def test_bbox_is_frozen() -> None:
    bbox = BBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4)
    with pytest.raises(Exception):
        bbox.x_min = 0.5  # type: ignore[misc]


def test_bbox_values() -> None:
    bbox = BBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4)
    assert bbox.x_min == 0.1
    assert bbox.y_min == 0.2
    assert bbox.x_max == 0.3
    assert bbox.y_max == 0.4


def test_ui_element_is_frozen() -> None:
    bbox = BBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4)
    element = UIElement(
        index=0,
        type="text",
        content="OK",
        bbox=bbox,
        center_x=0.2,
        center_y=0.3,
        interactivity=False,
    )
    with pytest.raises(Exception):
        element.index = 1  # type: ignore[misc]


def test_ui_element_fields() -> None:
    bbox = BBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4)
    element = UIElement(
        index=5,
        type="icon",
        content="Settings",
        bbox=bbox,
        center_x=0.2,
        center_y=0.3,
        interactivity=True,
    )
    assert element.index == 5
    assert element.type == "icon"
    assert element.content == "Settings"
    assert element.bbox == bbox
    assert element.center_x == 0.2
    assert element.center_y == 0.3
    assert element.interactivity is True


def test_pixel_bbox_is_frozen() -> None:
    bbox = PixelBBox(x_min=100, y_min=200, x_max=300, y_max=400)
    with pytest.raises(Exception):
        bbox.x_min = 50  # type: ignore[misc]


def test_pixel_bbox_values() -> None:
    bbox = PixelBBox(x_min=100, y_min=200, x_max=300, y_max=400)
    assert bbox.x_min == 100
    assert bbox.y_min == 200
    assert bbox.x_max == 300
    assert bbox.y_max == 400


def test_pixel_ui_element_fields() -> None:
    bbox = PixelBBox(x_min=100, y_min=200, x_max=300, y_max=400)
    element = PixelUIElement(
        index=2,
        type="text",
        content="Submit",
        bbox=bbox,
        center_x=200,
        center_y=300,
        interactivity=True,
    )
    assert element.index == 2
    assert element.type == "text"
    assert element.content == "Submit"
    assert element.center_x == 200
    assert element.center_y == 300
    assert element.interactivity is True


def test_parse_result_is_frozen() -> None:
    result = ParseResult(
        annotated_image="base64data",
        elements=(),
        image_width=1920,
        image_height=1080,
    )
    with pytest.raises(Exception):
        result.image_width = 800  # type: ignore[misc]


def test_parse_result_fields() -> None:
    bbox = BBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4)
    element = UIElement(
        index=0,
        type="text",
        content="OK",
        bbox=bbox,
        center_x=0.2,
        center_y=0.3,
        interactivity=False,
    )
    result = ParseResult(
        annotated_image="abc123",
        elements=(element,),
        image_width=1920,
        image_height=1080,
    )
    assert result.annotated_image == "abc123"
    assert len(result.elements) == 1
    assert result.elements[0].content == "OK"
    assert result.image_width == 1920
    assert result.image_height == 1080


def test_pixel_parse_result_is_frozen() -> None:
    result = PixelParseResult(
        annotated_image="base64data",
        elements=(),
        image_width=1920,
        image_height=1080,
    )
    with pytest.raises(Exception):
        result.image_height = 720  # type: ignore[misc]


def test_pixel_parse_result_fields() -> None:
    bbox = PixelBBox(x_min=192, y_min=216, x_max=576, y_max=432)
    element = PixelUIElement(
        index=0,
        type="icon",
        content="Close",
        bbox=bbox,
        center_x=384,
        center_y=324,
        interactivity=True,
    )
    result = PixelParseResult(
        annotated_image="xyz789",
        elements=(element,),
        image_width=1920,
        image_height=1080,
    )
    assert result.annotated_image == "xyz789"
    assert len(result.elements) == 1
    assert result.elements[0].content == "Close"
    assert result.elements[0].bbox.x_min == 192


def test_parse_result_elements_is_tuple() -> None:
    result = ParseResult(
        annotated_image="data",
        elements=(),
        image_width=800,
        image_height=600,
    )
    assert isinstance(result.elements, tuple)
