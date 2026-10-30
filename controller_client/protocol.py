import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Final

from controller_client.exceptions import ProtocolError, UnknownMessageTypeError

# Bumped whenever the wire protocol changes in a way an older controller cannot
# follow. The server ships this same file inside the controller ZIP, so the
# version here is always the one a freshly downloaded controller reports.
CLIENT_VERSION: Final[str] = "0.10.3"


class CloseCode(IntEnum):
    """Application WebSocket close codes the server uses to end a session for good."""

    # A newer Test Runner connected with the same key and took over the project.
    SUPERSEDED = 4001
    # A user pressed Force disconnect in the panel.
    FORCE_DISCONNECTED = 4002


# The controller stops reconnecting when the server closes with one of these.
TERMINAL_CLOSE_CODES: Final[frozenset[int]] = frozenset(CloseCode)


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
    DOUBLE_CLICK = "double_click"
    HOVER = "hover"
    DRAG = "drag"
    TYPE_TEXT = "type_text"
    KEY_PRESS = "key_press"
    SCREENSHOT_REQUEST = "screenshot_request"
    RUN_COMMAND = "run_command"
    PING = "ping"
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK = "browser_click"
    BROWSER_DOUBLE_CLICK = "browser_double_click"
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
    CLOSE_APP = "close_app"
    CLOSE_TAB = "close_tab"
    CHECK_APP_INSTALLED = "check_app_installed"
    CLEANUP_ENVIRONMENT = "cleanup_environment"
    SCROLL = "scroll"
    # Self-update (SELF_UPDATE_V1): server -> runner with the new bundle, and
    # runner -> server with the progress of that update.
    UPDATE_RUNNER = "update_runner"
    UPDATE_STATUS = "update_status"
    # Human in the loop person steps (PERSON_STEPS_V1): server -> runner with a
    # snapshot of the open steps, runner -> server with a person's answer, and
    # server -> runner with the outcome of that answer (same request_id).
    PERSON_STEP_STATE = "person_step_state"
    PERSON_STEP_ACTION = "person_step_action"
    PERSON_STEP_ACTION_RESULT = "person_step_action_result"
    # Ask AI (PERSON_STEP_ASSIST_V1): runner -> server with a person's question
    # about an open step, server -> runner with its acknowledgement (same
    # request_id). The answer itself arrives in the next ``person_step_state``.
    PERSON_STEP_QUESTION = "person_step_question"
    PERSON_STEP_QUESTION_RESULT = "person_step_question_result"


class ClientCapability(StrEnum):
    INTERACTIVE_COMMANDS_V1 = "interactive_commands_v1"
    CLEANUP_ENVIRONMENT_V1 = "cleanup_environment_v1"
    # Optional: browser actions carry a ``browser`` field naming the browser to
    # drive. Older controllers ignore the field and always launch Chromium.
    BROWSER_SELECTION_V1 = "browser_selection_v1"
    # Optional: ``double_click`` and ``browser_double_click``. Older controllers
    # answer UNKNOWN_COMMAND.
    DOUBLE_CLICK_V1 = "double_click_v1"
    # Optional: ``close_app`` and ``close_tab``. Older controllers answer
    # UNKNOWN_COMMAND.
    CLOSE_APP_V1 = "close_app_v1"
    # Optional: interactive commands run in ``cmd.exe`` on Windows. Older
    # controllers fail every command there; only Windows runners are checked.
    WINDOWS_SHELL_V1 = "windows_shell_v1"
    # Optional: ``scroll`` (mouse wheel). Older controllers answer
    # UNKNOWN_COMMAND.
    SCROLL_V1 = "scroll_v1"
    # Optional: ``update_runner`` / ``update_status`` (side-by-side self-update).
    # Checked by the server before it offers the Update button.
    SELF_UPDATE_V1 = "self_update_v1"
    # Optional: ``person_step_state`` / ``person_step_action`` /
    # ``person_step_action_result`` (the person step window). Advertised only
    # when the window is enabled and Tk works (``advertised_capabilities``).
    PERSON_STEPS_V1 = "person_steps_v1"
    # Optional: the window renders a step's ``how_to`` items and offers the Ask
    # AI box (``person_step_question`` / ``person_step_question_result``, the
    # snapshot's ``questions``). Advertised only together with PERSON_STEPS_V1.
    PERSON_STEP_ASSIST_V1 = "person_step_assist_v1"
    # Optional: the window renders a step's ``case_steps_label`` and the test
    # case steps it covers (``case_steps``); older windows get the label inside
    # ``step_label``. Advertised only together with PERSON_STEPS_V1.
    PERSON_STEP_CASE_STEPS_V1 = "person_step_case_steps_v1"


# Capabilities that need the person step window; dropped without it.
_PERSON_WINDOW_CAPABILITIES: Final[frozenset[ClientCapability]] = frozenset(
    {
        ClientCapability.PERSON_STEPS_V1,
        ClientCapability.PERSON_STEP_ASSIST_V1,
        ClientCapability.PERSON_STEP_CASE_STEPS_V1,
    }
)


# Everything this client build knows. The handshake sends
# ``advertised_capabilities(...)``, which may drop the runtime-dependent ones.
CLIENT_CAPABILITIES: Final[tuple[ClientCapability, ...]] = tuple(ClientCapability)


def advertised_capabilities(person_window: bool) -> tuple[ClientCapability, ...]:
    """The capabilities to send in the handshake.

    ``person_window`` is False when the person step window is disabled
    (``CONTROLLER_PERSON_WINDOW=false``) or Tk is unavailable; the server then
    never sends ``person_step_state`` and the person answers on the web.
    PERSON_STEP_ASSIST_V1 and PERSON_STEP_CASE_STEPS_V1 are dropped with it.
    """
    if person_window:
        return CLIENT_CAPABILITIES
    return tuple(
        capability
        for capability in CLIENT_CAPABILITIES
        if capability not in _PERSON_WINDOW_CAPABILITIES
    )


# What the server insists on; a controller missing any of them would
# authenticate fine and then silently time out on first use. Optional
# capabilities (BROWSER_SELECTION_V1, DOUBLE_CLICK_V1, CLOSE_APP_V1,
# WINDOWS_SHELL_V1, SCROLL_V1, SELF_UPDATE_V1, PERSON_STEPS_V1,
# PERSON_STEP_ASSIST_V1, PERSON_STEP_CASE_STEPS_V1) are checked
# where they are used instead.
REQUIRED_CLIENT_CAPABILITIES: Final[frozenset[ClientCapability]] = frozenset(
    {
        ClientCapability.INTERACTIVE_COMMANDS_V1,
        ClientCapability.CLEANUP_ENVIRONMENT_V1,
    }
)


class BrowserName(StrEnum):
    """Browsers the controller can drive; values are Playwright's own names.

    ``chrome`` and ``msedge`` are Playwright channels of the Chromium engine
    and use the browser installed on the test machine.
    """

    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"
    CHROME = "chrome"
    MSEDGE = "msedge"


class BrowserEngine(StrEnum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


BROWSER_ENGINES: Final[dict[BrowserName, BrowserEngine]] = {
    BrowserName.CHROMIUM: BrowserEngine.CHROMIUM,
    BrowserName.FIREFOX: BrowserEngine.FIREFOX,
    BrowserName.WEBKIT: BrowserEngine.WEBKIT,
    BrowserName.CHROME: BrowserEngine.CHROMIUM,
    BrowserName.MSEDGE: BrowserEngine.CHROMIUM,
}

BROWSER_DISPLAY_NAMES: Final[dict[BrowserName, str]] = {
    BrowserName.CHROMIUM: "Chromium",
    BrowserName.FIREFOX: "Firefox",
    BrowserName.WEBKIT: "WebKit (Safari engine)",
    BrowserName.CHROME: "Google Chrome",
    BrowserName.MSEDGE: "Microsoft Edge",
}

DEFAULT_BROWSER: Final[BrowserName] = BrowserName.CHROMIUM


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
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    INCOMPATIBLE_CLIENT = "INCOMPATIBLE_CLIENT"
    # Sent by the server to an action waiter when the controller socket dropped
    # before the reply arrived.
    CONTROLLER_DISCONNECTED = "CONTROLLER_DISCONNECTED"
    # The requested browser is not installed on the controller machine.
    BROWSER_NOT_INSTALLED = "BROWSER_NOT_INSTALLED"


@dataclass(frozen=True)
class HandshakePayload:
    api_key: str
    client_version: str
    capabilities: tuple[str, ...]
    system_info: dict[str, str | int]
    # Sent only by an old runner that reconnected while its own self-update
    # still runs (normalized UUID); absent otherwise, including on the new
    # runner an update started (it connects at the target version).
    update_in_flight_id: str | None = None
    # The person at this Test Runner (``CONTROLLER_PERSON_EMAIL``), normalized
    # by ``normalize_person_email``; absent when not configured. The server
    # trusts it only to name an active member of the key's project.
    person_email: str | None = None


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
class ClickPayload:
    x: int
    y: int
    button: str


@dataclass(frozen=True)
class DoubleClickPayload:
    x: int
    y: int


@dataclass(frozen=True)
class HoverPayload:
    x: int
    y: int


class ScrollDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


# Wheel notches per scroll message; the server's tool uses the default when the
# model gives no amount.
MAX_SCROLL_AMOUNT: Final[int] = 25
DEFAULT_SCROLL_AMOUNT: Final[int] = 5


@dataclass(frozen=True)
class ScrollPayload:
    direction: ScrollDirection
    amount: int
    # Both None: scroll at the current pointer position.
    x: int | None
    y: int | None


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
    browser: BrowserName


@dataclass(frozen=True)
class BrowserClickPayload:
    element_index: int
    browser: BrowserName


@dataclass(frozen=True)
class BrowserTypePayload:
    element_index: int
    text: str
    browser: BrowserName


@dataclass(frozen=True)
class BrowserHoverPayload:
    element_index: int
    browser: BrowserName


@dataclass(frozen=True)
class BrowserDownloadPayload:
    url: str
    save_path: str
    browser: BrowserName


@dataclass(frozen=True)
class BrowserTargetPayload:
    """Payload of the browser actions that only need to know which browser."""

    browser: BrowserName


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
class CloseAppPayload:
    app_name: str
    force: bool


@dataclass(frozen=True)
class CloseTabPayload:
    # The app whose frontmost tab is closed; the controller refuses when another
    # app is in front.
    app_name: str


@dataclass(frozen=True)
class CheckAppInstalledPayload:
    app_name: str


_MIB: Final[int] = 1024 * 1024
# Largest WebSocket message the runner accepts (``websockets`` ``max_size``).
MAX_MESSAGE_SIZE: Final[int] = 10 * _MIB
# Largest runner bundle (the ZIP, before base64) an ``update_runner`` may carry.
# Both sides must agree, so it lives here rather than in settings.
MAX_UPDATE_ARCHIVE_BYTES: Final[int] = 7 * _MIB
# Room for the rest of an ``update_runner`` message besides the base64 text.
_UPDATE_RUNNER_JSON_OVERHEAD_BYTES: Final[int] = 64 * 1024
# Base64 of n bytes is 4 * ceil(n / 3) characters; the whole message must fit
# in what the runner accepts.
assert (
    4 * -(-MAX_UPDATE_ARCHIVE_BYTES // 3) + _UPDATE_RUNNER_JSON_OVERHEAD_BYTES
    <= MAX_MESSAGE_SIZE
), "MAX_UPDATE_ARCHIVE_BYTES does not fit in MAX_MESSAGE_SIZE"
# The runner's whole self-update, from accepting ``update_runner`` to the
# handover (or ``failed``), inclusive. The server's update timeout must not be
# shorter.
UPDATE_MAX_SECONDS: Final[int] = 3600
# Longest ``detail`` of an ``update_status``; longer text is truncated.
UPDATE_STATUS_DETAIL_MAX_LENGTH: Final[int] = 500
# Longest version string ``version_tuple`` accepts (matches the server column).
MAX_VERSION_LENGTH: Final[int] = 32

_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


class UpdatePhase(StrEnum):
    """Progress of a self-update, sent by the runner in ``update_status``."""

    ACCEPTED = "accepted"
    # Verify the hash and extract the bundle into the new folder.
    PREPARING = "preparing"
    CREATING_ENVIRONMENT = "creating_environment"
    INSTALLING_DEPENDENCIES = "installing_dependencies"
    INSTALLING_BROWSERS = "installing_browsers"
    # The new runner process was spawned.
    STARTING = "starting"
    WAITING_FOR_NEW_RUNNER = "waiting_for_new_runner"
    # Terminal on the runner side; the old runner keeps running.
    FAILED = "failed"
    # Sent by the *new* runner after it connected; informational.
    OLD_REMOVED = "old_removed"
    OLD_REMOVAL_FAILED = "old_removal_failed"


@dataclass(frozen=True)
class UpdateRunnerPayload:
    # Server-side RunnerUpdate id (UUID string).
    update_id: str
    target_version: str
    archive_base64: str
    # Lowercase hex sha256 of the decoded archive.
    archive_sha256: str
    # Length in bytes of the decoded archive.
    archive_size: int


@dataclass(frozen=True)
class UpdateStatusPayload:
    update_id: str
    phase: UpdatePhase
    # Plain text, at most UPDATE_STATUS_DETAIL_MAX_LENGTH characters.
    detail: str


def version_tuple(version: str) -> tuple[int, ...] | None:
    """``"0.8.0"`` -> ``(0, 8, 0)``; ``None`` when the version cannot be parsed.

    Compare the tuples of both sides to tell which version is newer. Only dotted
    decimal versions of at most MAX_VERSION_LENGTH characters parse.
    """
    if len(version) > MAX_VERSION_LENGTH or not _VERSION_RE.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def serialize_message(
    message_type: MessageType,
    request_id: str | None = None,
    # object, not a narrower union: payloads carry nested lists and dicts
    # (the handshake's capabilities and system_info, for example).
    **payload: object,
) -> str:
    message: dict[str, object] = {
        "type": message_type,
        "request_id": request_id or str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    message.update(payload)
    return json.dumps(message)


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


def parse_click_payload(data: dict[str, object]) -> ClickPayload:
    return ClickPayload(
        x=_extract_int(data, "x"),
        y=_extract_int(data, "y"),
        button=_extract_str(data, "button", default="left"),
    )


def parse_double_click_payload(data: dict[str, object]) -> DoubleClickPayload:
    return DoubleClickPayload(
        x=_extract_int(data, "x"),
        y=_extract_int(data, "y"),
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


def parse_scroll_payload(data: dict[str, object]) -> ScrollPayload:
    raw_direction = _extract_str(data, "direction")
    try:
        direction = ScrollDirection(raw_direction)
    except ValueError as e:
        raise ProtocolError(f"Unknown scroll direction: {raw_direction}") from e
    amount = data.get("amount")
    if (
        not isinstance(amount, int)
        or isinstance(amount, bool)
        or not 1 <= amount <= MAX_SCROLL_AMOUNT
    ):
        raise ProtocolError(
            f"Invalid 'amount': expected an int from 1 to {MAX_SCROLL_AMOUNT}"
        )
    x = _extract_optional_int(data, "x")
    y = _extract_optional_int(data, "y")
    if (x is None) != (y is None):
        raise ProtocolError("'x' and 'y' must both be given or both be omitted")
    return ScrollPayload(direction=direction, amount=amount, x=x, y=y)


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


def _extract_browser(data: dict[str, object]) -> BrowserName:
    # A server older than this controller sends no browser: keep Chromium.
    raw = _extract_str(data, "browser", default=DEFAULT_BROWSER.value)
    try:
        return BrowserName(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown browser: {raw}") from e


def parse_browser_navigate_payload(data: dict[str, object]) -> BrowserNavigatePayload:
    return BrowserNavigatePayload(
        url=_extract_str(data, "url"),
        browser=_extract_browser(data),
    )


def parse_browser_click_payload(data: dict[str, object]) -> BrowserClickPayload:
    return BrowserClickPayload(
        element_index=_extract_int(data, "element_index"),
        browser=_extract_browser(data),
    )


def parse_browser_type_payload(data: dict[str, object]) -> BrowserTypePayload:
    return BrowserTypePayload(
        element_index=_extract_int(data, "element_index"),
        text=_extract_str(data, "text"),
        browser=_extract_browser(data),
    )


def parse_browser_hover_payload(data: dict[str, object]) -> BrowserHoverPayload:
    return BrowserHoverPayload(
        element_index=_extract_int(data, "element_index"),
        browser=_extract_browser(data),
    )


def parse_browser_download_payload(data: dict[str, object]) -> BrowserDownloadPayload:
    return BrowserDownloadPayload(
        url=_extract_str(data, "url"),
        save_path=_extract_str(data, "save_path", default=""),
        browser=_extract_browser(data),
    )


def parse_browser_target_payload(data: dict[str, object]) -> BrowserTargetPayload:
    return BrowserTargetPayload(browser=_extract_browser(data))


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
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"Invalid '{field}': expected int or null")
    return value


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


def parse_close_app_payload(data: dict[str, object]) -> CloseAppPayload:
    return CloseAppPayload(
        app_name=_extract_str(data, "app_name"),
        force=_extract_bool(data, "force", default=False),
    )


def parse_close_tab_payload(data: dict[str, object]) -> CloseTabPayload:
    return CloseTabPayload(
        app_name=_extract_str(data, "app_name"),
    )


def _normalized_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _extract_update_id(data: Mapping[str, object]) -> str:
    update_id = _normalized_uuid(data.get("update_id"))
    if update_id is None:
        raise ProtocolError("Missing or invalid 'update_id': expected a UUID")
    return update_id


def peek_update_id(data: Mapping[str, object]) -> str | None:
    """The normalized ``update_id`` of a message, or None if it has none.

    Lets the runner report a refused ``update_runner`` on its update row even
    when the rest of the message is invalid.
    """
    return _normalized_uuid(data.get("update_id"))


def parse_update_in_flight_id(data: Mapping[str, object]) -> str | None:
    """The handshake's optional ``update_in_flight_id`` (normalized UUID).

    None when absent or null. Raises ProtocolError for any other non-UUID value.
    """
    raw = data.get("update_in_flight_id")
    if raw is None:
        return None
    update_id = _normalized_uuid(raw)
    if update_id is None:
        raise ProtocolError("Invalid 'update_in_flight_id': expected a UUID")
    return update_id


def parse_update_runner_payload(data: dict[str, object]) -> UpdateRunnerPayload:
    """Validate an ``update_runner`` message; does not decode the archive.

    The size limit is enforced on both the declared size and the base64 text
    length, so an oversized bundle is refused before anything is decoded.
    """
    target_version = _extract_str(data, "target_version")
    if version_tuple(target_version) is None:
        raise ProtocolError(f"Invalid 'target_version': {target_version[:40]!r}")
    archive_size = data.get("archive_size")
    if (
        not isinstance(archive_size, int)
        or isinstance(archive_size, bool)
        or not 0 < archive_size <= MAX_UPDATE_ARCHIVE_BYTES
    ):
        raise ProtocolError(
            f"Invalid 'archive_size': expected 1 to {MAX_UPDATE_ARCHIVE_BYTES} bytes"
        )
    archive_base64 = _extract_str(data, "archive_base64")
    # Base64 of n bytes is 4 * ceil(n / 3) characters.
    if len(archive_base64) != 4 * -(-archive_size // 3):
        raise ProtocolError("'archive_base64' length does not match 'archive_size'")
    archive_sha256 = _extract_str(data, "archive_sha256").lower()
    if not _SHA256_HEX_RE.fullmatch(archive_sha256):
        raise ProtocolError("Invalid 'archive_sha256': expected 64 hex characters")
    return UpdateRunnerPayload(
        update_id=_extract_update_id(data),
        target_version=target_version,
        archive_base64=archive_base64,
        archive_sha256=archive_sha256,
        archive_size=archive_size,
    )


def parse_update_status_payload(data: dict[str, object]) -> UpdateStatusPayload:
    raw_phase = _extract_str(data, "phase")
    try:
        phase = UpdatePhase(raw_phase)
    except ValueError as e:
        raise ProtocolError(f"Unknown update phase: {raw_phase[:40]!r}") from e
    detail = _extract_str(data, "detail", default="")
    return UpdateStatusPayload(
        update_id=_extract_update_id(data),
        phase=phase,
        detail=detail[:UPDATE_STATUS_DETAIL_MAX_LENGTH],
    )


# ---------------------------------------------------------------------------
# Person at the Test Runner (handshake ``person_email``)
# ---------------------------------------------------------------------------

# RFC 5321 path limit; matches Django's EmailField.
PERSON_EMAIL_MAX_LENGTH: Final[int] = 254
# A deliberately simple check (one "@", a dot in the domain, no whitespace or
# control characters); the server decides by looking the address up.
_PERSON_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+"
)


def normalize_person_email(raw: str) -> str | None:
    """Strip and lower-case an email; None when blank.

    Raises ProtocolError when it is too long or not shaped like an email.
    The runner's config uses it to validate ``CONTROLLER_PERSON_EMAIL`` and
    the server's handshake uses it through ``parse_person_email``.
    """
    email = raw.strip().lower()
    if not email:
        return None
    if len(email) > PERSON_EMAIL_MAX_LENGTH:
        raise ProtocolError(
            f"Invalid person email: longer than {PERSON_EMAIL_MAX_LENGTH} characters"
        )
    if not _PERSON_EMAIL_RE.fullmatch(email):
        raise ProtocolError("Invalid person email: expected name@domain")
    return email


def parse_person_email(data: Mapping[str, object]) -> str | None:
    """The handshake's optional ``person_email``, normalized.

    None when absent, null or blank. Raises ProtocolError for a non-string,
    an over-long or a malformed value.
    """
    raw = data.get("person_email")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ProtocolError("Invalid 'person_email': expected a string")
    return normalize_person_email(raw)


# ---------------------------------------------------------------------------
# Person steps (PERSON_STEPS_V1)
# ---------------------------------------------------------------------------

# Most open steps one ``person_step_state`` carries (newest request first).
PERSON_STEP_STATE_MAX_STEPS: Final[int] = 5
# A person's answer: the server's limits (HELP_NOTE_MAX_LENGTH and
# PERSON_STEP_OUTPUT_MAX_LENGTH in projects.models), restated because the
# runner cannot import the server. Over-long answers are refused, not cut.
PERSON_STEP_NOTE_MAX_CHARS: Final[int] = 2000
PERSON_STEP_OUTPUT_MAX_CHARS: Final[int] = 20000
# Largest raw ``person_step_action`` message the server reads.
PERSON_STEP_ACTION_MAX_BYTES: Final[int] = 256 * 1024
# Display caps of the server-written state texts; longer text is cut when
# parsed (the server writes them within these limits).
PERSON_STEP_INSTRUCTION_MAX_CHARS: Final[int] = 2000
PERSON_STEP_EXPECTED_MAX_CHARS: Final[int] = 1000
PERSON_STEP_COMMAND_MAX_CHARS: Final[int] = 2000
PERSON_STEP_OBJECTION_MAX_CHARS: Final[int] = 1000
PERSON_STEP_LABEL_MAX_CHARS: Final[int] = 255
PERSON_STEP_URL_MAX_CHARS: Final[int] = 2000
PERSON_STEP_MAX_BLOCKERS: Final[int] = 10
# Longest user-safe ``message`` of a ``person_step_action_result``.
PERSON_STEP_RESULT_MESSAGE_MAX_CHARS: Final[int] = 1000
# "How to do it" items of a step (PERSON_STEP_ASSIST_V1); the server's
# PERSON_STEP_HOW_TO_* caps in projects.models. Cut when parsed.
PERSON_STEP_HOW_TO_MAX_ITEMS: Final[int] = 8
PERSON_STEP_HOW_TO_TEXT_MAX_CHARS: Final[int] = 400
PERSON_STEP_HOW_TO_COMMAND_MAX_CHARS: Final[int] = 500
# Test case steps a person step covers (PERSON_STEP_CASE_STEPS_V1); the
# server's PERSON_STEP_CASE_STEPS_* caps in projects.models. Cut when parsed.
PERSON_STEP_CASE_STEPS_MAX_ITEMS: Final[int] = 15
PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS: Final[int] = 1000
# Any positive step number the server stored; only the type is checked.
_PERSON_STEP_MAX_CASE_STEP_NUM: Final[int] = 2**31 - 1
# Ask AI: a question is refused when longer (PERSON_STEP_QUESTION_MAX_LENGTH
# on the server); a snapshot carries a step's newest questions, oldest first.
PERSON_STEP_QUESTION_MAX_CHARS: Final[int] = 1000
PERSON_STEP_ANSWER_MAX_CHARS: Final[int] = 4000
PERSON_STEP_STATE_MAX_QUESTIONS: Final[int] = 10
# Largest raw ``person_step_question`` message the server reads.
PERSON_STEP_QUESTION_MAX_BYTES: Final[int] = 16 * 1024
# ``CasePersonStep.round`` is a PositiveSmallIntegerField.
_PERSON_STEP_MAX_ROUND: Final[int] = 32767
_PERSON_STEP_MAX_ID: Final[int] = 2**63 - 1


class PersonStepWireStatus(StrEnum):
    """Open statuses a snapshot can carry (values of PersonStepStatus)."""

    WAITING = "waiting"
    NOT_CONFIRMED = "not_confirmed"


class PersonStepWireKind(StrEnum):
    """Values of PersonStepKind."""

    INTERACTIVE = "interactive"
    COMMAND = "command"
    RESTORE = "restore"


class PersonStepActionKind(StrEnum):
    """What the person answered from the window (the web card's four buttons)."""

    REPORT = "report"
    # Only while the step is ``not_confirmed``.
    OVERRULE = "overrule"
    FAIL = "fail"
    BLOCK = "block"


class PersonStepWireOutcome(StrEnum):
    """Values of PersonStepOutcome; ``report`` only."""

    AS_EXPECTED = "as_expected"
    DIFFERENT = "different"


class PersonStepQuestionWireStatus(StrEnum):
    """Values of PersonStepQuestionStatus."""

    PENDING = "pending"
    ANSWERED = "answered"
    FAILED = "failed"


@dataclass(frozen=True)
class PersonHowToPayload:
    """One numbered "How to do it" item; ``command`` is an AI suggestion."""

    text: str
    # One line for the step's shell; blank when the item has none.
    command: str = ""

    def to_wire(self) -> dict[str, object]:
        return {"text": self.text, "command": self.command}


@dataclass(frozen=True)
class PersonCaseStepPayload:
    """One numbered test case (TestRail) step a person step covers."""

    num: int
    text: str
    # The step's expected result; blank when the case gives none.
    expected: str = ""

    def to_wire(self) -> dict[str, object]:
        return {"num": self.num, "text": self.text, "expected": self.expected}


@dataclass(frozen=True)
class PersonStepQuestionPayload:
    """One Ask AI question of a step and its answer, as the window shows it."""

    question_id: int
    # The step round it was asked in.
    round: int
    status: PersonStepQuestionWireStatus
    question: str
    # Blank while pending; the user-safe failure text when failed.
    answer: str
    # Server-built display label of the asker (never runner-sent text).
    asked_by: str

    def to_wire(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "round": self.round,
            "status": self.status.value,
            "question": self.question,
            "answer": self.answer,
            "asked_by": self.asked_by,
        }


@dataclass(frozen=True)
class PersonStepStatePayload:
    """One open person step as the Test Runner window shows it."""

    person_step_id: int
    round: int
    status: PersonStepWireStatus
    run_name: str
    case_title: str
    # "Step N of M"; for a runner without PERSON_STEP_CASE_STEPS_V1 followed
    # by " · " and the case steps label.
    step_label: str
    kind: PersonStepWireKind
    instruction: str
    expected_result: str
    # Blank unless ``kind`` is command.
    command: str
    shell_label: str
    # The agent's reason while ``not_confirmed``; blank otherwise.
    objection: str
    # User-safe reasons the run cannot continue at once (run page gates).
    blockers: tuple[str, ...]
    # Absolute case page URL; blank when the server has no SITE_URL.
    case_url: str
    # PERSON_STEP_ASSIST_V1 only; empty from an older server.
    how_to: tuple[PersonHowToPayload, ...] = ()
    # The step's newest questions, oldest first (≤ PERSON_STEP_STATE_MAX_QUESTIONS).
    questions: tuple[PersonStepQuestionPayload, ...] = ()
    # PERSON_STEP_CASE_STEPS_V1 only; empty from an older server or when the
    # case has no numbered steps. "TestRail steps 3–5" / "Case step 2".
    case_steps_label: str = ""
    case_steps: tuple[PersonCaseStepPayload, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "person_step_id": self.person_step_id,
            "round": self.round,
            "status": self.status.value,
            "run_name": self.run_name,
            "case_title": self.case_title,
            "step_label": self.step_label,
            "kind": self.kind.value,
            "instruction": self.instruction,
            "expected_result": self.expected_result,
            "command": self.command,
            "shell_label": self.shell_label,
            "objection": self.objection,
            "blockers": list(self.blockers),
            "case_url": self.case_url,
            "how_to": [
                item.to_wire() for item in self.how_to[:PERSON_STEP_HOW_TO_MAX_ITEMS]
            ],
            "questions": [
                question.to_wire()
                for question in self.questions[-PERSON_STEP_STATE_MAX_QUESTIONS:]
            ],
            "case_steps_label": self.case_steps_label,
            "case_steps": [
                step.to_wire()
                for step in self.case_steps[:PERSON_STEP_CASE_STEPS_MAX_ITEMS]
            ],
        }


@dataclass(frozen=True)
class PersonStepStateMessage:
    # Newest request first; empty means "nothing for you; hide".
    steps: tuple[PersonStepStatePayload, ...]
    # The server takes ``person_step_question`` from this runner; the window
    # shows the Ask AI box only then (an older server never sets it).
    questions_enabled: bool = False


@dataclass(frozen=True)
class PersonStepActionPayload:
    action: PersonStepActionKind
    person_step_id: int
    # The round the person answered; a reopened step refuses an older round.
    round: int
    # Set for ``report`` only, None otherwise.
    outcome: PersonStepWireOutcome | None
    note: str
    # Pasted command output (report of a command step); blank otherwise.
    output: str


@dataclass(frozen=True)
class PersonStepActionResultPayload:
    ok: bool
    # User-safe text to show in the window.
    message: str
    # The run continues now (False: a gate refused; ``message`` says why).
    queued: bool


@dataclass(frozen=True)
class PersonStepQuestionAskPayload:
    """A person's question to the AI about an open step (``person_step_question``)."""

    person_step_id: int
    # The round the window showed; a reopened step refuses an older round.
    round: int
    # Stripped, non-empty, ≤ PERSON_STEP_QUESTION_MAX_CHARS.
    question: str


@dataclass(frozen=True)
class PersonStepQuestionResultPayload:
    """The acknowledgement of a question; the answer comes in a snapshot."""

    ok: bool
    # User-safe text to show in the window (why it was refused, or blank).
    message: str


def _extract_bounded_int(data: Mapping[str, object], field: str, maximum: int) -> int:
    value = data.get(field)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise ProtocolError(f"Missing or invalid '{field}': expected a positive int")
    return value


def _extract_text(
    data: Mapping[str, object], field: str, max_chars: int, *, truncate: bool
) -> str:
    """An optional string field ("" when absent); cut or refused when too long."""
    value = data.get(field, "")
    if not isinstance(value, str):
        raise ProtocolError(f"Invalid '{field}': expected a string")
    if len(value) > max_chars:
        if not truncate:
            raise ProtocolError(
                f"Invalid '{field}': longer than {max_chars} characters"
            )
        return value[:max_chars]
    return value


def _parse_person_step(raw: object) -> PersonStepStatePayload:
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid person step: expected an object")
    item: dict[str, object] = raw
    raw_status = _extract_str(item, "status")
    raw_kind = _extract_str(item, "kind")
    try:
        status = PersonStepWireStatus(raw_status)
        kind = PersonStepWireKind(raw_kind)
    except ValueError as e:
        raise ProtocolError(
            f"Unknown person step status or kind: {raw_status[:40]!r}, "
            f"{raw_kind[:40]!r}"
        ) from e
    raw_blockers = item.get("blockers", [])
    if not isinstance(raw_blockers, list) or not all(
        isinstance(blocker, str) for blocker in raw_blockers
    ):
        raise ProtocolError("Invalid 'blockers': expected a list of strings")
    blockers = tuple(
        blocker[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS]
        for blocker in raw_blockers[:PERSON_STEP_MAX_BLOCKERS]
    )
    return PersonStepStatePayload(
        person_step_id=_extract_bounded_int(
            item, "person_step_id", _PERSON_STEP_MAX_ID
        ),
        round=_extract_bounded_int(item, "round", _PERSON_STEP_MAX_ROUND),
        status=status,
        run_name=_extract_text(
            item, "run_name", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        case_title=_extract_text(
            item, "case_title", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        step_label=_extract_text(
            item, "step_label", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        kind=kind,
        instruction=_extract_text(
            item, "instruction", PERSON_STEP_INSTRUCTION_MAX_CHARS, truncate=True
        ),
        expected_result=_extract_text(
            item, "expected_result", PERSON_STEP_EXPECTED_MAX_CHARS, truncate=True
        ),
        command=_extract_text(
            item, "command", PERSON_STEP_COMMAND_MAX_CHARS, truncate=True
        ),
        shell_label=_extract_text(
            item, "shell_label", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        objection=_extract_text(
            item, "objection", PERSON_STEP_OBJECTION_MAX_CHARS, truncate=True
        ),
        blockers=blockers,
        case_url=_extract_text(
            item, "case_url", PERSON_STEP_URL_MAX_CHARS, truncate=True
        ),
        how_to=_parse_how_to(item.get("how_to", [])),
        questions=_parse_questions(item.get("questions", [])),
        case_steps_label=_extract_text(
            item, "case_steps_label", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        case_steps=_parse_case_steps(item.get("case_steps", [])),
    )


def _parse_case_steps(raw: object) -> tuple[PersonCaseStepPayload, ...]:
    # Absent from an older server; items beyond the cap dropped, texts cut.
    if not isinstance(raw, list):
        raise ProtocolError("Invalid 'case_steps': expected a list")
    items: list[PersonCaseStepPayload] = []
    for entry in raw[:PERSON_STEP_CASE_STEPS_MAX_ITEMS]:
        if not isinstance(entry, dict):
            raise ProtocolError("Invalid case step: expected an object")
        item: dict[str, object] = entry
        items.append(
            PersonCaseStepPayload(
                num=_extract_bounded_int(item, "num", _PERSON_STEP_MAX_CASE_STEP_NUM),
                text=_extract_text(
                    item, "text", PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS, truncate=True
                ),
                expected=_extract_text(
                    item,
                    "expected",
                    PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS,
                    truncate=True,
                ),
            )
        )
    return tuple(items)


def _parse_how_to(raw: object) -> tuple[PersonHowToPayload, ...]:
    # Absent from an older server; items beyond the cap dropped, texts cut.
    if not isinstance(raw, list):
        raise ProtocolError("Invalid 'how_to': expected a list")
    items: list[PersonHowToPayload] = []
    for entry in raw[:PERSON_STEP_HOW_TO_MAX_ITEMS]:
        if not isinstance(entry, dict):
            raise ProtocolError("Invalid how-to item: expected an object")
        item: dict[str, object] = entry
        items.append(
            PersonHowToPayload(
                text=_extract_text(
                    item, "text", PERSON_STEP_HOW_TO_TEXT_MAX_CHARS, truncate=True
                ),
                command=_extract_text(
                    item,
                    "command",
                    PERSON_STEP_HOW_TO_COMMAND_MAX_CHARS,
                    truncate=True,
                ),
            )
        )
    return tuple(items)


def _parse_questions(raw: object) -> tuple[PersonStepQuestionPayload, ...]:
    # Absent from an older server; oldest first, so the newest are kept.
    if not isinstance(raw, list):
        raise ProtocolError("Invalid 'questions': expected a list")
    return tuple(
        _parse_question(entry) for entry in raw[-PERSON_STEP_STATE_MAX_QUESTIONS:]
    )


def _parse_question(raw: object) -> PersonStepQuestionPayload:
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid question: expected an object")
    item: dict[str, object] = raw
    raw_status = _extract_str(item, "status")
    try:
        status = PersonStepQuestionWireStatus(raw_status)
    except ValueError as e:
        raise ProtocolError(f"Unknown question status: {raw_status[:40]!r}") from e
    return PersonStepQuestionPayload(
        question_id=_extract_bounded_int(item, "question_id", _PERSON_STEP_MAX_ID),
        round=_extract_bounded_int(item, "round", _PERSON_STEP_MAX_ROUND),
        status=status,
        question=_extract_text(
            item, "question", PERSON_STEP_QUESTION_MAX_CHARS, truncate=True
        ),
        answer=_extract_text(
            item, "answer", PERSON_STEP_ANSWER_MAX_CHARS, truncate=True
        ),
        asked_by=_extract_text(
            item, "asked_by", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
    )


def parse_person_step_state_payload(
    data: dict[str, object],
) -> PersonStepStateMessage:
    """Validate a ``person_step_state`` (runner side).

    Steps beyond PERSON_STEP_STATE_MAX_STEPS are dropped and over-long texts
    cut; a wrong type or an unknown status or kind raises ProtocolError.
    """
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raise ProtocolError("Missing or invalid 'steps': expected a list")
    return PersonStepStateMessage(
        steps=tuple(
            _parse_person_step(raw) for raw in raw_steps[:PERSON_STEP_STATE_MAX_STEPS]
        ),
        questions_enabled=_extract_bool(data, "questions_enabled", default=False),
    )


def serialize_person_step_state(
    steps: Sequence[PersonStepStatePayload], *, questions_enabled: bool = False
) -> str:
    """A ``person_step_state`` message (server side; fresh request_id).

    ``questions_enabled`` only for a runner with PERSON_STEP_ASSIST_V1.
    """
    return serialize_message(
        MessageType.PERSON_STEP_STATE,
        None,
        steps=[step.to_wire() for step in steps[:PERSON_STEP_STATE_MAX_STEPS]],
        questions_enabled=questions_enabled,
    )


def parse_person_step_action_payload(
    data: Mapping[str, object],
) -> PersonStepActionPayload:
    """Validate a ``person_step_action`` (both sides).

    Over-long note or output, a report without an outcome, and a "different"
    report without a note raise ProtocolError. ``outcome`` and ``output`` are
    dropped for actions other than ``report``. The note is stripped (as the
    web form does); the output is kept as pasted.
    """
    raw_action = data.get("action")
    if not isinstance(raw_action, str):
        raise ProtocolError("Missing or invalid 'action'")
    try:
        action = PersonStepActionKind(raw_action)
    except ValueError as e:
        raise ProtocolError(f"Unknown person step action: {raw_action[:40]!r}") from e
    note = _extract_text(
        data, "note", PERSON_STEP_NOTE_MAX_CHARS, truncate=False
    ).strip()
    output = _extract_text(data, "output", PERSON_STEP_OUTPUT_MAX_CHARS, truncate=False)
    outcome: PersonStepWireOutcome | None = None
    if action is PersonStepActionKind.REPORT:
        raw_outcome = data.get("outcome")
        if not isinstance(raw_outcome, str):
            raise ProtocolError("Missing or invalid 'outcome'")
        try:
            outcome = PersonStepWireOutcome(raw_outcome)
        except ValueError as e:
            raise ProtocolError(
                f"Unknown person step outcome: {raw_outcome[:40]!r}"
            ) from e
        if outcome is PersonStepWireOutcome.DIFFERENT and not note:
            raise ProtocolError("A different result needs a note")
    else:
        output = ""
    return PersonStepActionPayload(
        action=action,
        person_step_id=_extract_bounded_int(
            data, "person_step_id", _PERSON_STEP_MAX_ID
        ),
        round=_extract_bounded_int(data, "round", _PERSON_STEP_MAX_ROUND),
        outcome=outcome,
        note=note,
        output=output,
    )


def serialize_person_step_action(
    action: PersonStepActionPayload, request_id: str
) -> str:
    """A ``person_step_action`` message (runner side; one request_id per answer)."""
    return serialize_message(
        MessageType.PERSON_STEP_ACTION,
        request_id,
        action=action.action.value,
        person_step_id=action.person_step_id,
        round=action.round,
        outcome=action.outcome.value if action.outcome is not None else "",
        note=action.note,
        output=action.output,
    )


def parse_person_step_action_result_payload(
    data: dict[str, object],
) -> PersonStepActionResultPayload:
    """Validate a ``person_step_action_result`` (runner side)."""
    return PersonStepActionResultPayload(
        ok=_extract_bool(data, "ok"),
        message=_extract_text(
            data, "message", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
        queued=_extract_bool(data, "queued", default=False),
    )


def serialize_person_step_action_result(
    result: PersonStepActionResultPayload, request_id: str
) -> str:
    """A ``person_step_action_result`` (server side; echoes the action's id)."""
    return serialize_message(
        MessageType.PERSON_STEP_ACTION_RESULT,
        request_id,
        ok=result.ok,
        message=result.message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
        queued=result.queued,
    )


def parse_person_step_question_payload(
    data: Mapping[str, object],
) -> PersonStepQuestionAskPayload:
    """Validate a ``person_step_question`` (both sides).

    The question is stripped (as the web form does); an empty or over-long
    question raises ProtocolError (refused, not cut).
    """
    question = _extract_text(
        data, "question", PERSON_STEP_QUESTION_MAX_CHARS, truncate=False
    ).strip()
    if not question:
        raise ProtocolError("Missing 'question'")
    return PersonStepQuestionAskPayload(
        person_step_id=_extract_bounded_int(
            data, "person_step_id", _PERSON_STEP_MAX_ID
        ),
        round=_extract_bounded_int(data, "round", _PERSON_STEP_MAX_ROUND),
        question=question,
    )


def serialize_person_step_question(
    question: PersonStepQuestionAskPayload, request_id: str
) -> str:
    """A ``person_step_question`` message (runner side; one request_id per question)."""
    return serialize_message(
        MessageType.PERSON_STEP_QUESTION,
        request_id,
        person_step_id=question.person_step_id,
        round=question.round,
        question=question.question,
    )


def parse_person_step_question_result_payload(
    data: dict[str, object],
) -> PersonStepQuestionResultPayload:
    """Validate a ``person_step_question_result`` (runner side)."""
    return PersonStepQuestionResultPayload(
        ok=_extract_bool(data, "ok"),
        message=_extract_text(
            data, "message", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
    )


def serialize_person_step_question_result(
    result: PersonStepQuestionResultPayload, request_id: str
) -> str:
    """A ``person_step_question_result`` (server side; echoes the question's id)."""
    return serialize_message(
        MessageType.PERSON_STEP_QUESTION_RESULT,
        request_id,
        ok=result.ok,
        message=result.message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
    )


def parse_check_app_installed_payload(
    data: dict[str, object],
) -> CheckAppInstalledPayload:
    return CheckAppInstalledPayload(
        app_name=_extract_str(data, "app_name"),
    )
