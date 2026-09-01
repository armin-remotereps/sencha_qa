import dataclasses
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

from controller_client.exceptions import ProtocolError, UnknownMessageTypeError

# Bumped whenever the wire protocol changes in a way an older controller cannot
# follow. The server ships this same file inside the controller ZIP, so the
# version here is always the one a freshly downloaded controller reports.
CLIENT_VERSION: Final[str] = "0.2.0"


class MessageType(StrEnum):
    HANDSHAKE = "handshake"
    ACTION_RESULT = "action_result"
    SCREENSHOT_RESPONSE = "screenshot_response"
    COMMAND_OUTPUT = "command_output"
    COMMAND_RESULT = "command_result"
    BROWSER_CONTENT_RESULT = "browser_content_result"
    ERROR = "error"
    PONG = "pong"
    HANDSHAKE_ACK = "handshake_ack"
    CLICK = "click"
    HOVER = "hover"
    DRAG = "drag"
    TYPE_TEXT = "type_text"
    KEY_PRESS = "key_press"
    SCREENSHOT_REQUEST = "screenshot_request"
    RUN_COMMAND = "run_command"
    PING = "ping"
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK = "browser_click"
    BROWSER_TYPE = "browser_type"
    BROWSER_HOVER = "browser_hover"
    BROWSER_GET_ELEMENTS = "browser_get_elements"
    BROWSER_GET_PAGE_CONTENT = "browser_get_page_content"
    BROWSER_GET_URL = "browser_get_url"
    BROWSER_TAKE_SCREENSHOT = "browser_take_screenshot"
    BROWSER_DOWNLOAD = "browser_download"
    BROWSER_LIST_DOWNLOADS = "browser_list_downloads"
    START_INTERACTIVE_CMD = "start_interactive_cmd"
    SEND_INPUT = "send_input"
    TERMINATE_INTERACTIVE_CMD = "terminate_interactive_cmd"
    INTERACTIVE_OUTPUT = "interactive_output"
    WAIT_FOR_COMMAND = "wait_for_command"
    LAUNCH_APP = "launch_app"
    CHECK_APP_INSTALLED = "check_app_installed"
    CLEANUP_ENVIRONMENT = "cleanup_environment"
    FIND_ELEMENT = "find_element"
    FIND_ELEMENT_RESULT = "find_element_result"
    OMNIPARSER_STATUS = "omniparser_status"


class ClientCapability(StrEnum):
    FIND_ELEMENT_LOCAL_V1 = "find_element_local_v1"
    INTERACTIVE_COMMANDS_V1 = "interactive_commands_v1"
    CLEANUP_ENVIRONMENT_V1 = "cleanup_environment_v1"
    OMNIPARSER_STATUS_V1 = "omniparser_status_v1"


# What this client advertises in its handshake.
CLIENT_CAPABILITIES: Final[tuple[ClientCapability, ...]] = tuple(ClientCapability)

# What the server insists on; a controller missing any of them would
# authenticate fine and then silently time out on first use. Identical to
# CLIENT_CAPABILITIES today; they diverge once a capability becomes optional.
REQUIRED_CLIENT_CAPABILITIES: Final[frozenset[ClientCapability]] = frozenset(
    ClientCapability
)


class OmniParserState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


class MouseButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"


class StreamName(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class ErrorCode(StrEnum):
    INVALID_API_KEY = "INVALID_API_KEY"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    INVALID_MESSAGE = "INVALID_MESSAGE"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    SCREENSHOT_FAILED = "SCREENSHOT_FAILED"
    TIMEOUT = "TIMEOUT"
    FIND_ELEMENT_FAILED = "FIND_ELEMENT_FAILED"
    OMNIPARSER_NOT_READY = "OMNIPARSER_NOT_READY"
    INCOMPATIBLE_CLIENT = "INCOMPATIBLE_CLIENT"


@dataclass(frozen=True)
class HandshakePayload:
    api_key: str
    client_version: str
    capabilities: tuple[str, ...]
    system_info: dict[str, str | int]


@dataclass(frozen=True)
class ActionResultPayload:
    success: bool
    message: str
    duration_ms: float


@dataclass(frozen=True)
class ScreenshotResponsePayload:
    success: bool
    image_base64: str
    width: int
    height: int
    format: str


@dataclass(frozen=True)
class ErrorPayload:
    code: str
    message: str
    details: str


@dataclass(frozen=True)
class HandshakeAckPayload:
    status: str
    message: str
    project_id: str
    project_name: str


@dataclass(frozen=True)
class OmniParserStatusPayload:
    state: OmniParserState
    message: str
    device: str
    weights_dir: str
    phase: str
    load_seconds: float


@dataclass(frozen=True)
class ClickPayload:
    x: int
    y: int
    button: str


@dataclass(frozen=True)
class HoverPayload:
    x: int
    y: int


@dataclass(frozen=True)
class DragPayload:
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    button: str
    duration: float


@dataclass(frozen=True)
class TypeTextPayload:
    text: str
    interval: float


@dataclass(frozen=True)
class KeyPressPayload:
    keys: str


@dataclass(frozen=True)
class RunCommandPayload:
    command: str


@dataclass(frozen=True)
class CommandOutputPayload:
    line: str
    stream: StreamName


@dataclass(frozen=True)
class CommandResultPayload:
    success: bool
    stdout: str
    stderr: str
    return_code: int
    duration_ms: float


@dataclass(frozen=True)
class BrowserNavigatePayload:
    url: str


@dataclass(frozen=True)
class BrowserClickPayload:
    element_index: int


@dataclass(frozen=True)
class BrowserTypePayload:
    element_index: int
    text: str


@dataclass(frozen=True)
class BrowserHoverPayload:
    element_index: int


@dataclass(frozen=True)
class BrowserDownloadPayload:
    url: str
    save_path: str


@dataclass(frozen=True)
class BrowserContentResultPayload:
    success: bool
    content: str
    duration_ms: float


@dataclass(frozen=True)
class StartInteractiveCmdPayload:
    command: str


@dataclass(frozen=True)
class SendInputPayload:
    session_id: str
    input_text: str


@dataclass(frozen=True)
class TerminateInteractiveCmdPayload:
    session_id: str


@dataclass(frozen=True)
class InteractiveOutputPayload:
    session_id: str
    output: str
    is_alive: bool
    exit_code: int | None
    duration_ms: float


@dataclass(frozen=True)
class WaitForCommandPayload:
    session_id: str


@dataclass(frozen=True)
class LaunchAppPayload:
    app_name: str


@dataclass(frozen=True)
class CheckAppInstalledPayload:
    app_name: str


@dataclass(frozen=True)
class FindElementPayload:
    box_threshold: float | None
    iou_threshold: float | None


@dataclass(frozen=True)
class PixelBBoxPayload:
    x_min: int
    y_min: int
    x_max: int
    y_max: int


@dataclass(frozen=True)
class PixelElementPayload:
    index: int
    type: str
    content: str
    bbox: PixelBBoxPayload
    center_x: int
    center_y: int
    interactivity: bool


@dataclass(frozen=True)
class FindElementResultPayload:
    success: bool
    annotated_image_base64: str
    elements: tuple[PixelElementPayload, ...]
    image_width: int
    image_height: int


def serialize_message(
    message_type: MessageType,
    request_id: str | None = None,
    # object, not a narrower union: find_element results carry nested
    # lists of element dicts, which the original union couldn't express.
    **payload: object,
) -> str:
    message: dict[str, object] = {
        "type": message_type,
        "request_id": request_id or str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    message.update(payload)
    return json.dumps(message)


def serialize_find_element_result(
    request_id: str, result: FindElementResultPayload
) -> str:
    return serialize_message(
        MessageType.FIND_ELEMENT_RESULT,
        request_id=request_id,
        success=result.success,
        annotated_image_base64=result.annotated_image_base64,
        elements=[dataclasses.asdict(element) for element in result.elements],
        image_width=result.image_width,
        image_height=result.image_height,
    )


def peek_request_id(raw: str) -> str | None:
    """Best-effort ``request_id`` lookup for messages that failed to parse.

    Lets the client answer a malformed or unknown message with an ``error``
    the server can correlate, instead of leaving it to wait for a timeout.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    request_id = data.get("request_id")
    return request_id if isinstance(request_id, str) else None


def deserialize_server_message(raw: str) -> tuple[MessageType, str, dict[str, object]]:
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"Invalid JSON: {e}") from e

    raw_type = data.get("type")
    if not isinstance(raw_type, str):
        raise ProtocolError("Missing or invalid 'type' field")

    try:
        message_type = MessageType(raw_type)
    except ValueError as e:
        raise UnknownMessageTypeError(f"Unknown message type: {raw_type}") from e

    request_id = data.get("request_id")
    if not isinstance(request_id, str):
        raise ProtocolError("Missing or invalid 'request_id' field")

    return message_type, request_id, data


def _extract_str(
    data: dict[str, object], field: str, default: str | None = None
) -> str:
    value = data.get(field, default)
    if not isinstance(value, str):
        raise ProtocolError(f"Missing or invalid '{field}'")
    return value


def _extract_int(
    data: dict[str, object], field: str, default: int | None = None
) -> int:
    value = data.get(field, default)
    if not isinstance(value, int):
        raise ProtocolError(f"Missing or invalid '{field}'")
    return value


def _extract_number(
    data: dict[str, object], field: str, default: float | None = None
) -> float:
    value = data.get(field, default)
    if not isinstance(value, (int, float)):
        raise ProtocolError(f"Missing or invalid '{field}'")
    return float(value)


def parse_handshake_ack_payload(data: dict[str, object]) -> HandshakeAckPayload:
    return HandshakeAckPayload(
        status=_extract_str(data, "status"),
        message=_extract_str(data, "message", default=""),
        project_id=_extract_str(data, "project_id", default=""),
        project_name=_extract_str(data, "project_name", default=""),
    )


def parse_handshake_capabilities(data: dict[str, object]) -> tuple[str, ...]:
    raw = data.get("capabilities", [])
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise ProtocolError("Invalid 'capabilities': expected a list of strings")
    return tuple(raw)


def parse_omniparser_status_payload(
    data: dict[str, object],
) -> OmniParserStatusPayload:
    raw_state = _extract_str(data, "state")
    try:
        state = OmniParserState(raw_state)
    except ValueError as e:
        raise ProtocolError(f"Unknown OmniParser state: {raw_state}") from e
    return OmniParserStatusPayload(
        state=state,
        message=_extract_str(data, "message", default=""),
        device=_extract_str(data, "device", default=""),
        weights_dir=_extract_str(data, "weights_dir", default=""),
        phase=_extract_str(data, "phase", default=""),
        load_seconds=_extract_number(data, "load_seconds", default=0.0),
    )


def parse_click_payload(data: dict[str, object]) -> ClickPayload:
    return ClickPayload(
        x=_extract_int(data, "x"),
        y=_extract_int(data, "y"),
        button=_extract_str(data, "button", default="left"),
    )


def parse_hover_payload(data: dict[str, object]) -> HoverPayload:
    return HoverPayload(
        x=_extract_int(data, "x"),
        y=_extract_int(data, "y"),
    )


def parse_drag_payload(data: dict[str, object]) -> DragPayload:
    return DragPayload(
        start_x=_extract_int(data, "start_x"),
        start_y=_extract_int(data, "start_y"),
        end_x=_extract_int(data, "end_x"),
        end_y=_extract_int(data, "end_y"),
        button=_extract_str(data, "button", default="left"),
        duration=_extract_number(data, "duration", default=0.5),
    )


def parse_type_text_payload(data: dict[str, object]) -> TypeTextPayload:
    return TypeTextPayload(
        text=_extract_str(data, "text"),
        interval=_extract_number(data, "interval", default=0.0),
    )


def parse_key_press_payload(data: dict[str, object]) -> KeyPressPayload:
    return KeyPressPayload(
        keys=_extract_str(data, "keys"),
    )


def parse_run_command_payload(data: dict[str, object]) -> RunCommandPayload:
    return RunCommandPayload(
        command=_extract_str(data, "command"),
    )


def parse_browser_navigate_payload(data: dict[str, object]) -> BrowserNavigatePayload:
    return BrowserNavigatePayload(
        url=_extract_str(data, "url"),
    )


def parse_browser_click_payload(data: dict[str, object]) -> BrowserClickPayload:
    return BrowserClickPayload(
        element_index=_extract_int(data, "element_index"),
    )


def parse_browser_type_payload(data: dict[str, object]) -> BrowserTypePayload:
    return BrowserTypePayload(
        element_index=_extract_int(data, "element_index"),
        text=_extract_str(data, "text"),
    )


def parse_browser_hover_payload(data: dict[str, object]) -> BrowserHoverPayload:
    return BrowserHoverPayload(
        element_index=_extract_int(data, "element_index"),
    )


def parse_browser_download_payload(data: dict[str, object]) -> BrowserDownloadPayload:
    return BrowserDownloadPayload(
        url=_extract_str(data, "url"),
        save_path=_extract_str(data, "save_path", default=""),
    )


def _extract_bool(
    data: dict[str, object], field: str, default: bool | None = None
) -> bool:
    value = data.get(field, default)
    if not isinstance(value, bool):
        raise ProtocolError(f"Missing or invalid '{field}'")
    return value


def _extract_optional_int(data: dict[str, object], field: str) -> int | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ProtocolError(f"Invalid '{field}': expected int or null")
    return value


def _extract_optional_number(data: dict[str, object], field: str) -> float | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ProtocolError(f"Invalid '{field}': expected number or null")
    return float(value)


def parse_start_interactive_cmd_payload(
    data: dict[str, object],
) -> StartInteractiveCmdPayload:
    return StartInteractiveCmdPayload(
        command=_extract_str(data, "command"),
    )


def parse_send_input_payload(data: dict[str, object]) -> SendInputPayload:
    return SendInputPayload(
        session_id=_extract_str(data, "session_id"),
        input_text=_extract_str(data, "input_text"),
    )


def parse_terminate_interactive_cmd_payload(
    data: dict[str, object],
) -> TerminateInteractiveCmdPayload:
    return TerminateInteractiveCmdPayload(
        session_id=_extract_str(data, "session_id"),
    )


def parse_wait_for_command_payload(data: dict[str, object]) -> WaitForCommandPayload:
    return WaitForCommandPayload(
        session_id=_extract_str(data, "session_id"),
    )


def parse_launch_app_payload(data: dict[str, object]) -> LaunchAppPayload:
    return LaunchAppPayload(
        app_name=_extract_str(data, "app_name"),
    )


def parse_check_app_installed_payload(
    data: dict[str, object],
) -> CheckAppInstalledPayload:
    return CheckAppInstalledPayload(
        app_name=_extract_str(data, "app_name"),
    )


def parse_find_element_payload(data: dict[str, object]) -> FindElementPayload:
    return FindElementPayload(
        box_threshold=_extract_optional_number(data, "box_threshold"),
        iou_threshold=_extract_optional_number(data, "iou_threshold"),
    )
