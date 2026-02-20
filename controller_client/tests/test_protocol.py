import json
from unittest.mock import patch

import pytest

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    ClickPayload,
    CommandResultPayload,
    DragPayload,
    ErrorCode,
    HoverPayload,
    InteractiveOutputPayload,
    KeyPressPayload,
    MessageType,
    MouseButton,
    RunCommandPayload,
    SendInputPayload,
    StartInteractiveCmdPayload,
    TerminateInteractiveCmdPayload,
    TypeTextPayload,
    _extract_bool,
    _extract_optional_int,
    deserialize_server_message,
    parse_click_payload,
    parse_drag_payload,
    parse_handshake_ack_payload,
    parse_hover_payload,
    parse_key_press_payload,
    parse_run_command_payload,
    parse_send_input_payload,
    parse_start_interactive_cmd_payload,
    parse_terminate_interactive_cmd_payload,
    parse_type_text_payload,
    serialize_message,
)


class TestMessageType:
    def test_client_to_server_types(self) -> None:
        assert MessageType.HANDSHAKE.value == "handshake"
        assert MessageType.ACTION_RESULT.value == "action_result"
        assert MessageType.SCREENSHOT_RESPONSE.value == "screenshot_response"
        assert MessageType.ERROR.value == "error"
        assert MessageType.PONG.value == "pong"

    def test_server_to_client_types(self) -> None:
        assert MessageType.HANDSHAKE_ACK.value == "handshake_ack"
        assert MessageType.CLICK.value == "click"
        assert MessageType.HOVER.value == "hover"
        assert MessageType.DRAG.value == "drag"
        assert MessageType.TYPE_TEXT.value == "type_text"
        assert MessageType.KEY_PRESS.value == "key_press"
        assert MessageType.SCREENSHOT_REQUEST.value == "screenshot_request"
        assert MessageType.RUN_COMMAND.value == "run_command"
        assert MessageType.PING.value == "ping"

    def test_client_command_result_type(self) -> None:
        assert MessageType.COMMAND_RESULT.value == "command_result"


class TestMouseButton:
    def test_values(self) -> None:
        assert MouseButton.LEFT.value == "left"
        assert MouseButton.RIGHT.value == "right"


class TestErrorCode:
    def test_values(self) -> None:
        assert ErrorCode.INVALID_API_KEY.value == "INVALID_API_KEY"
        assert ErrorCode.EXECUTION_FAILED.value == "EXECUTION_FAILED"
        assert ErrorCode.UNKNOWN_COMMAND.value == "UNKNOWN_COMMAND"


class TestSerializeMessage:
    def test_basic_serialization(self) -> None:
        raw = serialize_message(MessageType.PONG, request_id="abc-123")
        data = json.loads(raw)
        assert data["type"] == "pong"
        assert data["request_id"] == "abc-123"
        assert "timestamp" in data

    def test_with_payload(self) -> None:
        raw = serialize_message(
            MessageType.ACTION_RESULT,
            request_id="req-1",
            success=True,
            message="done",
            duration_ms=42.5,
        )
        data = json.loads(raw)
        assert data["success"] is True
        assert data["message"] == "done"
        assert data["duration_ms"] == 42.5

    def test_generates_request_id_when_none(self) -> None:
        raw = serialize_message(MessageType.PONG)
        data = json.loads(raw)
        assert len(data["request_id"]) > 0


class TestDeserializeServerMessage:
    def test_valid_message(self) -> None:
        raw = json.dumps({"type": "click", "request_id": "r1", "x": 100, "y": 200})
        msg_type, req_id, data = deserialize_server_message(raw)
        assert msg_type == MessageType.CLICK
        assert req_id == "r1"
        assert data["x"] == 100

    def test_invalid_json(self) -> None:
        with pytest.raises(ProtocolError, match="Invalid JSON"):
            deserialize_server_message("not json")

    def test_missing_type(self) -> None:
        raw = json.dumps({"request_id": "r1"})
        with pytest.raises(ProtocolError, match="type"):
            deserialize_server_message(raw)

    def test_unknown_type(self) -> None:
        raw = json.dumps({"type": "unknown_xyz", "request_id": "r1"})
        with pytest.raises(ProtocolError, match="Unknown message type"):
            deserialize_server_message(raw)

    def test_missing_request_id(self) -> None:
        raw = json.dumps({"type": "ping"})
        with pytest.raises(ProtocolError, match="request_id"):
            deserialize_server_message(raw)


class TestParseHandshakeAckPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {
            "status": "ok",
            "project_id": "p1",
            "project_name": "My Project",
        }
        ack = parse_handshake_ack_payload(data)
        assert ack.status == "ok"
        assert ack.project_id == "p1"
        assert ack.project_name == "My Project"

    def test_missing_status(self) -> None:
        data: dict[str, object] = {"project_id": "p1", "project_name": "P"}
        with pytest.raises(ProtocolError, match="status"):
            parse_handshake_ack_payload(data)


class TestParseClickPayload:
    def test_valid_with_defaults(self) -> None:
        data: dict[str, object] = {"x": 10, "y": 20}
        payload = parse_click_payload(data)
        assert payload == ClickPayload(x=10, y=20, button="left")

    def test_valid_with_right_button(self) -> None:
        data: dict[str, object] = {"x": 10, "y": 20, "button": "right"}
        payload = parse_click_payload(data)
        assert payload.button == "right"

    def test_missing_x(self) -> None:
        data: dict[str, object] = {"y": 20}
        with pytest.raises(ProtocolError, match="x"):
            parse_click_payload(data)


class TestParseHoverPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {"x": 50, "y": 60}
        payload = parse_hover_payload(data)
        assert payload == HoverPayload(x=50, y=60)

    def test_missing_y(self) -> None:
        data: dict[str, object] = {"x": 50}
        with pytest.raises(ProtocolError, match="y"):
            parse_hover_payload(data)


class TestParseDragPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {
            "start_x": 0,
            "start_y": 0,
            "end_x": 100,
            "end_y": 100,
        }
        payload = parse_drag_payload(data)
        assert payload == DragPayload(
            start_x=0, start_y=0, end_x=100, end_y=100, button="left", duration=0.5
        )

    def test_missing_end_x(self) -> None:
        data: dict[str, object] = {"start_x": 0, "start_y": 0, "end_y": 100}
        with pytest.raises(ProtocolError, match="end_x"):
            parse_drag_payload(data)


class TestParseTypeTextPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {"text": "hello", "interval": 0.05}
        payload = parse_type_text_payload(data)
        assert payload == TypeTextPayload(text="hello", interval=0.05)

    def test_default_interval(self) -> None:
        data: dict[str, object] = {"text": "hi"}
        payload = parse_type_text_payload(data)
        assert payload.interval == 0.0

    def test_missing_text(self) -> None:
        data: dict[str, object] = {"interval": 0.1}
        with pytest.raises(ProtocolError, match="text"):
            parse_type_text_payload(data)


class TestParseKeyPressPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {"keys": "ctrl+c"}
        payload = parse_key_press_payload(data)
        assert payload == KeyPressPayload(keys="ctrl+c")

    def test_missing_keys(self) -> None:
        data: dict[str, object] = {}
        with pytest.raises(ProtocolError, match="keys"):
            parse_key_press_payload(data)


class TestParseRunCommandPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {"command": "echo hello"}
        payload = parse_run_command_payload(data)
        assert payload == RunCommandPayload(command="echo hello")

    def test_missing_command(self) -> None:
        data: dict[str, object] = {}
        with pytest.raises(ProtocolError, match="command"):
            parse_run_command_payload(data)


class TestCommandResultPayload:
    def test_creation(self) -> None:
        payload = CommandResultPayload(
            success=True,
            stdout="hello\n",
            stderr="",
            return_code=0,
            duration_ms=42.5,
        )
        assert payload.success is True
        assert payload.stdout == "hello\n"
        assert payload.stderr == ""
        assert payload.return_code == 0
        assert payload.duration_ms == 42.5

    def test_frozen(self) -> None:
        payload = CommandResultPayload(
            success=False, stdout="", stderr="err", return_code=1, duration_ms=0.0
        )
        with pytest.raises(AttributeError):
            payload.success = True  # type: ignore[misc]


class TestInteractiveMessageTypes:
    def test_interactive_message_types(self) -> None:
        assert MessageType.START_INTERACTIVE_CMD.value == "start_interactive_cmd"
        assert MessageType.SEND_INPUT.value == "send_input"
        assert (
            MessageType.TERMINATE_INTERACTIVE_CMD.value == "terminate_interactive_cmd"
        )
        assert MessageType.INTERACTIVE_OUTPUT.value == "interactive_output"


class TestExtractBool:
    def test_valid_true(self) -> None:
        data: dict[str, object] = {"is_alive": True}
        assert _extract_bool(data, "is_alive") is True

    def test_valid_false(self) -> None:
        data: dict[str, object] = {"is_alive": False}
        assert _extract_bool(data, "is_alive") is False

    def test_default(self) -> None:
        data: dict[str, object] = {}
        assert _extract_bool(data, "is_alive", default=True) is True

    def test_missing_raises(self) -> None:
        data: dict[str, object] = {}
        with pytest.raises(ProtocolError, match="is_alive"):
            _extract_bool(data, "is_alive")

    def test_wrong_type_raises(self) -> None:
        data: dict[str, object] = {"is_alive": "yes"}
        with pytest.raises(ProtocolError, match="is_alive"):
            _extract_bool(data, "is_alive")


class TestExtractOptionalInt:
    def test_valid_int(self) -> None:
        data: dict[str, object] = {"exit_code": 0}
        assert _extract_optional_int(data, "exit_code") == 0

    def test_none(self) -> None:
        data: dict[str, object] = {"exit_code": None}
        assert _extract_optional_int(data, "exit_code") is None

    def test_missing(self) -> None:
        data: dict[str, object] = {}
        assert _extract_optional_int(data, "exit_code") is None

    def test_wrong_type_raises(self) -> None:
        data: dict[str, object] = {"exit_code": "zero"}
        with pytest.raises(ProtocolError, match="exit_code"):
            _extract_optional_int(data, "exit_code")


class TestStartInteractiveCmdPayload:
    def test_creation(self) -> None:
        payload = StartInteractiveCmdPayload(command="sudo apt install -y nginx")
        assert payload.command == "sudo apt install -y nginx"

    def test_frozen(self) -> None:
        payload = StartInteractiveCmdPayload(command="test")
        with pytest.raises(AttributeError):
            payload.command = "other"  # type: ignore[misc]


class TestSendInputPayload:
    def test_creation(self) -> None:
        payload = SendInputPayload(session_id="abc-123", input_text="password")
        assert payload.session_id == "abc-123"
        assert payload.input_text == "password"


class TestTerminateInteractiveCmdPayload:
    def test_creation(self) -> None:
        payload = TerminateInteractiveCmdPayload(session_id="abc-123")
        assert payload.session_id == "abc-123"


class TestInteractiveOutputPayload:
    def test_creation(self) -> None:
        payload = InteractiveOutputPayload(
            session_id="s1",
            output="some output",
            is_alive=True,
            exit_code=None,
            duration_ms=42.5,
        )
        assert payload.session_id == "s1"
        assert payload.output == "some output"
        assert payload.is_alive is True
        assert payload.exit_code is None
        assert payload.duration_ms == 42.5

    def test_with_exit_code(self) -> None:
        payload = InteractiveOutputPayload(
            session_id="s2",
            output="",
            is_alive=False,
            exit_code=0,
            duration_ms=100.0,
        )
        assert payload.exit_code == 0
        assert payload.is_alive is False


class TestParseStartInteractiveCmdPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {"command": "sudo apt install -y nginx"}
        payload = parse_start_interactive_cmd_payload(data)
        assert payload.command == "sudo apt install -y nginx"

    def test_missing_command(self) -> None:
        data: dict[str, object] = {}
        with pytest.raises(ProtocolError, match="command"):
            parse_start_interactive_cmd_payload(data)


class TestParseSendInputPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {"session_id": "s1", "input_text": "y"}
        payload = parse_send_input_payload(data)
        assert payload.session_id == "s1"
        assert payload.input_text == "y"

    def test_missing_session_id(self) -> None:
        data: dict[str, object] = {"input_text": "y"}
        with pytest.raises(ProtocolError, match="session_id"):
            parse_send_input_payload(data)

    def test_missing_input_text(self) -> None:
        data: dict[str, object] = {"session_id": "s1"}
        with pytest.raises(ProtocolError, match="input_text"):
            parse_send_input_payload(data)


class TestParseTerminateInteractiveCmdPayload:
    def test_valid(self) -> None:
        data: dict[str, object] = {"session_id": "s1"}
        payload = parse_terminate_interactive_cmd_payload(data)
        assert payload.session_id == "s1"

    def test_missing_session_id(self) -> None:
        data: dict[str, object] = {}
        with pytest.raises(ProtocolError, match="session_id"):
            parse_terminate_interactive_cmd_payload(data)
