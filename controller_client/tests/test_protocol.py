from __future__ import annotations

import json

from controller_client.protocol import (
    FindElementResultPayload,
    MessageType,
    PixelBBoxPayload,
    PixelElementPayload,
    parse_find_element_payload,
    serialize_message,
)


def test_parse_find_element_payload_with_both_thresholds() -> None:
    payload = parse_find_element_payload({"box_threshold": 0.1, "iou_threshold": 0.5})
    assert payload.box_threshold == 0.1
    assert payload.iou_threshold == 0.5


def test_parse_find_element_payload_with_missing_thresholds() -> None:
    payload = parse_find_element_payload({})
    assert payload.box_threshold is None
    assert payload.iou_threshold is None


def test_parse_find_element_payload_with_null_thresholds() -> None:
    payload = parse_find_element_payload({"box_threshold": None, "iou_threshold": None})
    assert payload.box_threshold is None
    assert payload.iou_threshold is None


def test_parse_find_element_payload_accepts_int_as_number() -> None:
    payload = parse_find_element_payload({"box_threshold": 1, "iou_threshold": 0})
    assert payload.box_threshold == 1.0
    assert payload.iou_threshold == 0.0


def test_serialize_message_round_trips_nested_find_element_result() -> None:
    element = PixelElementPayload(
        index=0,
        type="icon",
        content="Submit button",
        bbox=PixelBBoxPayload(x_min=10, y_min=20, x_max=30, y_max=40),
        center_x=20,
        center_y=30,
        interactivity=True,
    )
    result = FindElementResultPayload(
        success=True,
        annotated_image_base64="fake-base64",
        elements=(element,),
        image_width=1920,
        image_height=1080,
    )

    raw = serialize_message(
        MessageType.FIND_ELEMENT_RESULT,
        request_id="req-1",
        success=result.success,
        annotated_image_base64=result.annotated_image_base64,
        elements=[
            {
                "index": e.index,
                "type": e.type,
                "content": e.content,
                "bbox": {
                    "x_min": e.bbox.x_min,
                    "y_min": e.bbox.y_min,
                    "x_max": e.bbox.x_max,
                    "y_max": e.bbox.y_max,
                },
                "center_x": e.center_x,
                "center_y": e.center_y,
                "interactivity": e.interactivity,
            }
            for e in result.elements
        ],
        image_width=result.image_width,
        image_height=result.image_height,
    )

    data = json.loads(raw)
    assert data["type"] == "find_element_result"
    assert data["success"] is True
    assert data["image_width"] == 1920
    assert data["elements"][0]["bbox"]["x_max"] == 30
    assert data["elements"][0]["content"] == "Submit button"
