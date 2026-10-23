"""Wire protocol between the server and a vision worker.

Standard library only: the server imports this module directly (as it does
``controller_client.protocol``), so it must never pull in torch or the
vendored OmniParser code.
"""

import dataclasses
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Final

from vision_worker.exceptions import ProtocolError, UnknownMessageTypeError

# Bumped whenever the wire protocol changes in a way an older worker cannot follow.
WORKER_VERSION: Final[str] = "1.0.0"

# The worker re-sends its status this often; the server treats a worker it has
# not heard from in VISION_WORKER_STALE_SECONDS as gone.
HEARTBEAT_INTERVAL_SECONDS: Final[int] = 30

# How long either side waits for the other half of the handshake.
HANDSHAKE_ACK_TIMEOUT_SECONDS: Final[int] = 30

# Matches Daphne's --websocket-max-message-size flag.
MAX_MESSAGE_BYTES: Final[int] = 32 * 1024 * 1024

# Match the server's VisionWorker.name and .client_version columns.
MAX_WORKER_NAME_LENGTH: Final[int] = 100
MAX_WORKER_VERSION_LENGTH: Final[int] = 32

# Handshake fields the server stores and shows on every project page; anything
# else, or anything longer, is dropped so a worker cannot bloat those pages.
SYSTEM_INFO_KEYS: Final[frozenset[str]] = frozenset(
    {"hostname", "os", "os_version", "python", "torch", "cuda", "gpu", "vram_mb"}
)
MAX_SYSTEM_INFO_VALUE_LENGTH: Final[int] = 200
MAX_CAPABILITIES: Final[int] = 32
MAX_CAPABILITY_LENGTH: Final[int] = 64


class CloseCode(IntEnum):
    """Application WebSocket close codes that end a worker session for good."""

    # A newer connection with the same worker name took over.
    SUPERSEDED = 4001


# The worker stops reconnecting when the server closes with one of these.
TERMINAL_CLOSE_CODES: Final[frozenset[int]] = frozenset(CloseCode)


class MessageType(StrEnum):
    HANDSHAKE = "handshake"
    HANDSHAKE_ACK = "handshake_ack"
    # Also the heartbeat.
    STATUS = "status"
    PARSE_REQUEST = "parse_request"
    PARSE_RESULT = "parse_result"
    ERROR = "error"


class WorkerCapability(StrEnum):
    PARSE_V1 = "parse_v1"


# What this worker advertises in its handshake.
WORKER_CAPABILITIES: Final[tuple[WorkerCapability, ...]] = tuple(WorkerCapability)

REQUIRED_WORKER_CAPABILITIES: Final[frozenset[WorkerCapability]] = frozenset(
    {WorkerCapability.PARSE_V1}
)


class WorkerState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


class HandshakeStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    INCOMPATIBLE = "incompatible"


class ErrorCode(StrEnum):
    INVALID_TOKEN = "INVALID_TOKEN"
    INCOMPATIBLE = "INCOMPATIBLE"
    INVALID_MESSAGE = "INVALID_MESSAGE"
    # The model has not finished loading, or failed to load.
    NOT_READY = "NOT_READY"
    # The worker's queue is full; the server tries another worker.
    BUSY = "BUSY"
    # The request waited in the worker's queue past its timeout and was dropped
    # without being parsed.
    EXPIRED = "EXPIRED"
    PARSE_FAILED = "PARSE_FAILED"
    # Sent by the server to a parse waiter when the worker socket dropped
    # before the reply arrived. Never sent by a worker.
    WORKER_DISCONNECTED = "WORKER_DISCONNECTED"


@dataclass(frozen=True)
class HandshakePayload:
    token: str
    worker_name: str
    worker_version: str
    capabilities: tuple[str, ...]
    # hostname, os, python, torch, gpu, vram_mb, ...
    system_info: dict[str, str | int]


@dataclass(frozen=True)
class HandshakeAckPayload:
    status: HandshakeStatus
    message: str


@dataclass(frozen=True)
class StatusPayload:
    state: WorkerState
    message: str
    device: str
    phase: str
    load_seconds: float


@dataclass(frozen=True)
class ParseRequestPayload:
    image_base64: str
    box_threshold: float | None
    iou_threshold: float | None
    # How long the server waits for this reply; a request still queued after
    # this long is answered EXPIRED instead of being parsed.
    timeout_seconds: float


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
class ParseResultPayload:
    annotated_image_base64: str
    # Pixel coordinates of the image that was sent, even when the annotated
    # copy was downscaled to fit the message size limit.
    elements: tuple[PixelElementPayload, ...]
    image_width: int
    image_height: int
    ocr_seconds: float
    som_seconds: float
    queue_seconds: float


@dataclass(frozen=True)
class ErrorPayload:
    code: str
    message: str
    details: str


def serialize_message(
    message_type: MessageType,
    request_id: str | None = None,
    **payload: object,
) -> str:
    message: dict[str, object] = {
        "type": message_type,
        "request_id": request_id or str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    message.update(payload)
    return json.dumps(message)


def serialize_parse_result(request_id: str, result: ParseResultPayload) -> str:
    return serialize_message(
        MessageType.PARSE_RESULT,
        request_id=request_id,
        annotated_image_base64=result.annotated_image_base64,
        elements=[dataclasses.asdict(element) for element in result.elements],
        image_width=result.image_width,
        image_height=result.image_height,
        ocr_seconds=result.ocr_seconds,
        som_seconds=result.som_seconds,
        queue_seconds=result.queue_seconds,
    )


def deserialize_message(raw: str) -> tuple[MessageType, str, dict[str, object]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"Invalid JSON: {e}") from e
    except RecursionError as e:
        raise ProtocolError("Invalid JSON: nested too deeply") from e
    if not isinstance(data, dict):
        raise ProtocolError("Message is not a JSON object")

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


def _extract_int(data: dict[str, object], field: str) -> int:
    value = data.get(field)
    # bool is an int subclass; a coordinate of True is malformed.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"Missing or invalid '{field}'")
    return value


def _extract_bool(
    data: dict[str, object], field: str, default: bool | None = None
) -> bool:
    value = data.get(field, default)
    if not isinstance(value, bool):
        raise ProtocolError(f"Missing or invalid '{field}'")
    return value


def _extract_number(
    data: dict[str, object], field: str, default: float | None = None
) -> float:
    value = data.get(field, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProtocolError(f"Missing or invalid '{field}'")
    return float(value)


def _extract_optional_number(data: dict[str, object], field: str) -> float | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProtocolError(f"Invalid '{field}': expected number or null")
    return float(value)


def _extract_dict(data: dict[str, object], field: str) -> dict[str, object]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ProtocolError(f"Missing or invalid '{field}'")
    return value


def parse_handshake_payload(data: dict[str, object]) -> HandshakePayload:
    raw_capabilities = data.get("capabilities", [])
    if (
        not isinstance(raw_capabilities, list)
        or len(raw_capabilities) > MAX_CAPABILITIES
        or not all(
            isinstance(c, str) and len(c) <= MAX_CAPABILITY_LENGTH
            for c in raw_capabilities
        )
    ):
        raise ProtocolError("Invalid 'capabilities': expected a short list of strings")

    raw_info = data.get("system_info", {})
    if not isinstance(raw_info, dict):
        raise ProtocolError("Invalid 'system_info': expected an object")
    system_info: dict[str, str | int] = {}
    for key, value in raw_info.items():
        if key not in SYSTEM_INFO_KEYS:
            continue
        if isinstance(value, str):
            system_info[key] = value[:MAX_SYSTEM_INFO_VALUE_LENGTH]
        elif isinstance(value, int) and not isinstance(value, bool):
            system_info[key] = value

    worker_name = _extract_str(data, "worker_name").strip()
    if not worker_name:
        raise ProtocolError("Empty 'worker_name'")
    if len(worker_name) > MAX_WORKER_NAME_LENGTH:
        raise ProtocolError(
            f"'worker_name' is longer than {MAX_WORKER_NAME_LENGTH} characters"
        )

    worker_version = _extract_str(data, "worker_version")
    if len(worker_version) > MAX_WORKER_VERSION_LENGTH:
        raise ProtocolError(
            f"'worker_version' is longer than {MAX_WORKER_VERSION_LENGTH} characters"
        )

    return HandshakePayload(
        token=_extract_str(data, "token"),
        worker_name=worker_name,
        worker_version=worker_version,
        capabilities=tuple(raw_capabilities),
        system_info=system_info,
    )


def parse_handshake_ack_payload(data: dict[str, object]) -> HandshakeAckPayload:
    raw_status = _extract_str(data, "status")
    try:
        status = HandshakeStatus(raw_status)
    except ValueError as e:
        raise ProtocolError(f"Unknown handshake status: {raw_status}") from e
    return HandshakeAckPayload(
        status=status,
        message=_extract_str(data, "message", default=""),
    )


def parse_status_payload(data: dict[str, object]) -> StatusPayload:
    raw_state = _extract_str(data, "state")
    try:
        state = WorkerState(raw_state)
    except ValueError as e:
        raise ProtocolError(f"Unknown worker state: {raw_state}") from e
    return StatusPayload(
        state=state,
        message=_extract_str(data, "message", default=""),
        device=_extract_str(data, "device", default=""),
        phase=_extract_str(data, "phase", default=""),
        load_seconds=_extract_number(data, "load_seconds", default=0.0),
    )


def parse_parse_request_payload(data: dict[str, object]) -> ParseRequestPayload:
    return ParseRequestPayload(
        image_base64=_extract_str(data, "image_base64"),
        box_threshold=_extract_optional_number(data, "box_threshold"),
        iou_threshold=_extract_optional_number(data, "iou_threshold"),
        timeout_seconds=_extract_number(data, "timeout_seconds"),
    )


def _parse_pixel_element(raw: object) -> PixelElementPayload:
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid element: expected an object")
    bbox = _extract_dict(raw, "bbox")
    return PixelElementPayload(
        index=_extract_int(raw, "index"),
        type=_extract_str(raw, "type", default=""),
        content=_extract_str(raw, "content", default=""),
        bbox=PixelBBoxPayload(
            x_min=_extract_int(bbox, "x_min"),
            y_min=_extract_int(bbox, "y_min"),
            x_max=_extract_int(bbox, "x_max"),
            y_max=_extract_int(bbox, "y_max"),
        ),
        center_x=_extract_int(raw, "center_x"),
        center_y=_extract_int(raw, "center_y"),
        interactivity=_extract_bool(raw, "interactivity", default=False),
    )


def parse_parse_result_payload(data: dict[str, object]) -> ParseResultPayload:
    raw_elements = data.get("elements")
    if not isinstance(raw_elements, list):
        raise ProtocolError("Missing or invalid 'elements'")
    return ParseResultPayload(
        annotated_image_base64=_extract_str(data, "annotated_image_base64"),
        elements=tuple(_parse_pixel_element(el) for el in raw_elements),
        image_width=_extract_int(data, "image_width"),
        image_height=_extract_int(data, "image_height"),
        ocr_seconds=_extract_number(data, "ocr_seconds", default=0.0),
        som_seconds=_extract_number(data, "som_seconds", default=0.0),
        queue_seconds=_extract_number(data, "queue_seconds", default=0.0),
    )


def parse_error_payload(data: dict[str, object]) -> ErrorPayload:
    return ErrorPayload(
        code=_extract_str(data, "code"),
        message=_extract_str(data, "message", default=""),
        details=_extract_str(data, "details", default=""),
    )
