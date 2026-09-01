from __future__ import annotations

import json

import pytest

from controller_client.exceptions import ProtocolError, UnknownMessageTypeError
from controller_client.protocol import (
    CLIENT_CAPABILITIES,
    CLIENT_VERSION,
    FindElementResultPayload,
    MessageType,
    OmniParserState,
    PixelBBoxPayload,
    PixelElementPayload,
    deserialize_server_message,
    parse_find_element_payload,
    parse_handshake_ack_payload,
    parse_handshake_capabilities,
    parse_omniparser_status_payload,
    peek_request_id,
    serialize_find_element_result,
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


def test_handshake_message_carries_version_and_capabilities() -> None:
    raw = serialize_message(
        MessageType.HANDSHAKE,
        api_key="k",
        client_version=CLIENT_VERSION,
        capabilities=[c.value for c in CLIENT_CAPABILITIES],
        system_info={},
    )

    data = json.loads(raw)
    assert data["client_version"] == "0.2.0"
    assert data["capabilities"] == [
        "find_element_local_v1",
        "interactive_commands_v1",
        "cleanup_environment_v1",
        "omniparser_status_v1",
    ]


def test_parse_handshake_capabilities_valid() -> None:
    assert parse_handshake_capabilities({"capabilities": ["a", "b"]}) == ("a", "b")


def test_parse_handshake_capabilities_missing_is_empty() -> None:
    assert parse_handshake_capabilities({}) == ()


def test_parse_handshake_capabilities_invalid_raises() -> None:
    with pytest.raises(ProtocolError, match="capabilities"):
        parse_handshake_capabilities({"capabilities": "find_element_local_v1"})
    with pytest.raises(ProtocolError, match="capabilities"):
        parse_handshake_capabilities({"capabilities": ["ok", 3]})


def test_parse_omniparser_status_payload_valid() -> None:
    payload = parse_omniparser_status_payload(
        {
            "state": "ready",
            "message": "OmniParser ready on cpu",
            "device": "cpu",
            "weights_dir": "/w",
            "phase": "model_load",
            "load_seconds": 12,
        }
    )

    assert payload.state is OmniParserState.READY
    assert payload.device == "cpu"
    assert payload.weights_dir == "/w"
    assert payload.phase == "model_load"
    assert payload.load_seconds == 12.0


def test_parse_omniparser_status_payload_unknown_state_raises() -> None:
    with pytest.raises(ProtocolError, match="Unknown OmniParser state"):
        parse_omniparser_status_payload({"state": "warming_up"})


def test_parse_handshake_ack_payload_includes_message() -> None:
    ack = parse_handshake_ack_payload(
        {"status": "incompatible", "message": "Controller too old"}
    )

    assert ack.status == "incompatible"
    assert ack.message == "Controller too old"
    assert ack.project_id == ""
    assert ack.project_name == ""


def test_deserialize_unknown_type_raises_unknown_message_type_error() -> None:
    with pytest.raises(UnknownMessageTypeError, match="teleport"):
        deserialize_server_message('{"type": "teleport", "request_id": "r1"}')


def test_peek_request_id() -> None:
    assert peek_request_id('{"type": "teleport", "request_id": "r1"}') == "r1"
    assert peek_request_id('{"type": "teleport"}') is None
    assert peek_request_id("not json") is None
    assert peek_request_id("[1, 2]") is None


def test_serialize_find_element_result_round_trip() -> None:
    element = PixelElementPayload(
        index=1,
        type="text",
        content="Login",
        bbox=PixelBBoxPayload(x_min=1, y_min=2, x_max=3, y_max=4),
        center_x=2,
        center_y=3,
        interactivity=False,
    )
    result = FindElementResultPayload(
        success=True,
        annotated_image_base64="abc=",
        elements=(element,),
        image_width=800,
        image_height=600,
    )

    data = json.loads(serialize_find_element_result("req-7", result))

    assert data["type"] == "find_element_result"
    assert data["request_id"] == "req-7"
    assert data["annotated_image_base64"] == "abc="
    assert data["image_width"] == 800
    assert data["image_height"] == 600
    assert data["elements"] == [
        {
            "index": 1,
            "type": "text",
            "content": "Login",
            "bbox": {"x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4},
            "center_x": 2,
            "center_y": 3,
            "interactivity": False,
        }
    ]
