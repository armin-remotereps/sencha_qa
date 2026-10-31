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
CLIENT_VERSION: Final[str] = "0.12.0"


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
    # Person window v2: runner -> server with a pause of a case
    # (CASE_PAUSE_V1), a discussion turn or a help resolution
    # (PERSON_DISCUSSION_V1); server -> runner with the outcome (same
    # request_id). New state arrives in the next ``person_step_state``.
    CASE_PAUSE_ACTION = "case_pause_action"
    CASE_PAUSE_ACTION_RESULT = "case_pause_action_result"
    PERSON_CHAT_SEND = "person_chat_send"
    PERSON_HELP_ACTION = "person_help_action"
    PERSON_CASE_ACTION_RESULT = "person_case_action_result"
    # Artifact scan (ARTIFACT_SCAN_V1): server -> runner with ``scan_directory``
    # or ``inspect_paths``, runner -> server with ``artifact_scan_result``
    # (same request_id) carrying the result object under ``result``.
    SCAN_DIRECTORY = "scan_directory"
    INSPECT_PATHS = "inspect_paths"
    ARTIFACT_SCAN_RESULT = "artifact_scan_result"


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
    # Optional: the snapshot's ``cases`` (test case text and log tail) and the
    # one-tap report. Advertised only together with PERSON_STEPS_V1.
    PERSON_CASE_CONTEXT_V1 = "person_case_context_v1"
    # Optional: help cases in ``cases`` with their discussion, and
    # ``person_chat_send`` / ``person_help_action``. Advertised only together
    # with PERSON_STEPS_V1.
    PERSON_DISCUSSION_V1 = "person_discussion_v1"
    # Optional: the pause fields of ``cases`` and ``case_pause_action``.
    # Advertised only together with PERSON_STEPS_V1.
    CASE_PAUSE_V1 = "case_pause_v1"
    # Optional: ``scan_directory`` and ``inspect_paths`` (read-only file system
    # inventory, answered with ``artifact_scan_result``). Older controllers
    # answer UNKNOWN_COMMAND.
    ARTIFACT_SCAN_V1 = "artifact_scan_v1"


# Capabilities that need the person step window; dropped without it.
_PERSON_WINDOW_CAPABILITIES: Final[frozenset[ClientCapability]] = frozenset(
    {
        ClientCapability.PERSON_STEPS_V1,
        ClientCapability.PERSON_STEP_ASSIST_V1,
        ClientCapability.PERSON_STEP_CASE_STEPS_V1,
        ClientCapability.PERSON_CASE_CONTEXT_V1,
        ClientCapability.PERSON_DISCUSSION_V1,
        ClientCapability.CASE_PAUSE_V1,
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
    PERSON_STEP_ASSIST_V1, PERSON_STEP_CASE_STEPS_V1, PERSON_CASE_CONTEXT_V1,
    PERSON_DISCUSSION_V1 and CASE_PAUSE_V1 are dropped with it.
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
# PERSON_STEP_ASSIST_V1, PERSON_STEP_CASE_STEPS_V1, ARTIFACT_SCAN_V1,
# PERSON_CASE_CONTEXT_V1, PERSON_DISCUSSION_V1, CASE_PAUSE_V1) are
# checked where they are used instead.
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
    # The step's run case (``PersonCaseStatePayload.pivot_id``); 0 from a
    # server older than 0.12.0. Older runners ignore the key.
    pivot_id: int = 0

    def to_wire(self) -> dict[str, object]:
        return {
            "person_step_id": self.person_step_id,
            "pivot_id": self.pivot_id,
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
    # PERSON_CASE_CONTEXT_V1 only: the cases behind the steps, an open help
    # request and the running case (≤ PERSON_CASE_STATE_MAX_CASES).
    cases: tuple["PersonCaseStatePayload", ...] = ()


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
        pivot_id=_extract_non_negative_int(item, "pivot_id"),
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
        cases=_parse_cases(data.get("cases", [])),
    )


def serialize_person_step_state(
    steps: Sequence[PersonStepStatePayload],
    *,
    questions_enabled: bool = False,
    cases: Sequence["PersonCaseStatePayload"] | None = None,
) -> str:
    """A ``person_step_state`` message (server side; fresh request_id).

    ``questions_enabled`` only for a runner with PERSON_STEP_ASSIST_V1;
    ``cases`` only for one with PERSON_CASE_CONTEXT_V1 (None: the key is left
    out, as an older server does).
    """
    extra: dict[str, object] = {}
    if cases is not None:
        extra["cases"] = [
            case.to_wire() for case in cases[:PERSON_CASE_STATE_MAX_CASES]
        ]
    return serialize_message(
        MessageType.PERSON_STEP_STATE,
        None,
        steps=[step.to_wire() for step in steps[:PERSON_STEP_STATE_MAX_STEPS]],
        questions_enabled=questions_enabled,
        **extra,
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


# ---------------------------------------------------------------------------
# Person window v2: cases, discussion and pause (PERSON_CASE_CONTEXT_V1,
# PERSON_DISCUSSION_V1, CASE_PAUSE_V1)
# ---------------------------------------------------------------------------

# Most cases one snapshot carries (a step's case, a help case, the running one).
PERSON_CASE_STATE_MAX_CASES: Final[int] = 3
# Each test case block (preconditions, steps, expected results).
PERSON_CASE_TEXT_MAX_CHARS: Final[int] = 8000
# Numbered steps of a steps-template case (for highlighting the covered ones).
PERSON_CASE_NUMBERED_STEPS_MAX_ITEMS: Final[int] = 50
# The case log tail: whole lines, newest kept.
PERSON_CASE_LOG_TAIL_MAX_CHARS: Final[int] = 16000
PERSON_CASE_LOG_TAIL_MAX_LINES: Final[int] = 200
# The help request's reason and ask, and the agent's proposal note.
PERSON_HELP_TEXT_MAX_CHARS: Final[int] = 4000
# The discussion: the newest display rows, each text cut.
PERSON_DISCUSSION_MAX_MESSAGES: Final[int] = 40
PERSON_DISCUSSION_MESSAGE_MAX_CHARS: Final[int] = 4000
# The agent's pending plan redesign.
PERSON_PLAN_DRAFT_MAX_ROWS: Final[int] = 30
PERSON_PLAN_DRAFT_TEXT_MAX_CHARS: Final[int] = 1000
# Runner -> server texts: refused (not cut) when longer. The server's
# SETUP_MESSAGE_MAX_LENGTH, HELP_NOTE_MAX_LENGTH and PAUSE_MESSAGE_MAX_LENGTH.
PERSON_CHAT_TEXT_MAX_CHARS: Final[int] = 8000
PERSON_HELP_NOTE_MAX_CHARS: Final[int] = 2000
CASE_PAUSE_MESSAGE_MAX_CHARS: Final[int] = 8000
# Largest raw ``case_pause_action`` / ``person_chat_send`` /
# ``person_help_action`` message the server reads.
PERSON_CASE_ACTION_MAX_BYTES: Final[int] = 64 * 1024
_PERSON_CASE_ISO_MAX_CHARS: Final[int] = 64


class PersonCaseRole(StrEnum):
    """Why a case is in the snapshot."""

    # An open person step of the case waits (referenced by ``steps``).
    STEP = "step"
    # The case's open help request waits for a person (no live auto-fix).
    HELP = "help"
    # The case runs; it only enables Pause and never makes the window show.
    RUNNING = "running"


class ContinueModeWire(StrEnum):
    """Values of HelpContinueMode."""

    CURRENT_SCREEN = "current_screen"
    RESTORE = "restore"
    FROM_SCRATCH = "from_scratch"


class PersonHelpActionKind(StrEnum):
    """How a person resolves a help request from the window."""

    CONTINUE = "continue"
    MARK_FAILED = "mark_failed"
    MARK_BLOCKED = "mark_blocked"


class DiscussionMessageKind(StrEnum):
    """Display rows of a discussion (tool rows become notices)."""

    USER = "user"
    ASSISTANT = "assistant"
    NOTICE = "notice"


@dataclass(frozen=True)
class PersonCasePausePayload:
    """The case's pause state (CASE_PAUSE_V1)."""

    # The Pause button applies now.
    can_pause: bool
    # Why it does not (user-safe); "" when it can or a pause is pending.
    pause_blocker: str
    # A pause was requested; the case has not stopped yet.
    pause_pending: bool
    pause_requested_by: str
    # The case's open help request was opened by a person's pause.
    paused: bool
    paused_by: str
    # PauseMessageState of the first message ("" when none) and its note.
    pause_message_state: str
    pause_message_note: str

    def to_wire(self) -> dict[str, object]:
        return {
            "can_pause": self.can_pause,
            "pause_blocker": self.pause_blocker,
            "pause_pending": self.pause_pending,
            "pause_requested_by": self.pause_requested_by,
            "paused": self.paused,
            "paused_by": self.paused_by,
            "pause_message_state": self.pause_message_state,
            "pause_message_note": self.pause_message_note,
        }


@dataclass(frozen=True)
class PersonDiscussionMessagePayload:
    """One display row of the case's discussion thread."""

    message_id: int
    kind: DiscussionMessageKind
    # Server-built display name ("" for the agent and notices).
    author: str
    text: str
    # ISO 8601 creation time.
    created_at: str

    def to_wire(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "kind": self.kind.value,
            "author": self.author,
            "text": self.text,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class PersonPlanDraftRowPayload:
    """One step of the agent's redesigned plan."""

    num: int
    description: str
    expected_result: str
    # The earlier step it carries (passed, not re-run); 0 when it runs.
    carried_from: int
    # "Carried from step 2" / "Runs" (server text).
    state_label: str

    def to_wire(self) -> dict[str, object]:
        return {
            "num": self.num,
            "description": self.description,
            "expected_result": self.expected_result,
            "carried_from": self.carried_from,
            "state_label": self.state_label,
        }


@dataclass(frozen=True)
class PersonPlanDraftPayload:
    """The help-chat agent's pending redesign of the case's plan."""

    revision_id: int
    reason: str
    resume_step: int
    carried_count: int
    rows: tuple[PersonPlanDraftRowPayload, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "reason": self.reason,
            "resume_step": self.resume_step,
            "carried_count": self.carried_count,
            "rows": [row.to_wire() for row in self.rows[:PERSON_PLAN_DRAFT_MAX_ROWS]],
        }


@dataclass(frozen=True)
class PersonHelpPayload:
    """The case's open help request and its discussion (PERSON_DISCUSSION_V1)."""

    help_request_id: int
    step_num: int
    reason: str
    ask: str
    # The infrastructure fault's label; "" when the agent asked.
    fault_label: str
    # Who paused the case; "" unless a person did.
    paused_by: str
    # The stored mode and the modes Continue may use (display order).
    continue_mode: ContinueModeWire
    continue_mode_choices: tuple[ContinueModeWire, ...]
    # The agent's suggested mode; None when it suggests none.
    agent_continue_mode: ContinueModeWire | None
    can_continue: bool
    # User-safe reasons the run cannot continue now.
    blockers: tuple[str, ...]
    # The agent's suggestion ("Continue", "Mark failed", ...; "" when none).
    proposal_label: str
    proposal_note: str
    plan_draft: PersonPlanDraftPayload | None
    # Absolute discussion URL ("" before it opens or without SITE_URL).
    thread_url: str
    # An agent turn is queued or running.
    turn_live: bool
    can_send: bool
    # Why a message cannot be sent now ("" when it can).
    send_blocker: str
    # Newest rows, oldest first.
    messages: tuple[PersonDiscussionMessagePayload, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "help_request_id": self.help_request_id,
            "step_num": self.step_num,
            "reason": self.reason,
            "ask": self.ask,
            "fault_label": self.fault_label,
            "paused_by": self.paused_by,
            "continue_mode": self.continue_mode.value,
            "continue_mode_choices": [
                mode.value for mode in self.continue_mode_choices
            ],
            "agent_continue_mode": (
                self.agent_continue_mode.value
                if self.agent_continue_mode is not None
                else ""
            ),
            "can_continue": self.can_continue,
            "blockers": list(self.blockers[:PERSON_STEP_MAX_BLOCKERS]),
            "proposal_label": self.proposal_label,
            "proposal_note": self.proposal_note,
            "plan_draft": (
                self.plan_draft.to_wire() if self.plan_draft is not None else None
            ),
            "thread_url": self.thread_url,
            "turn_live": self.turn_live,
            "can_send": self.can_send,
            "send_blocker": self.send_blocker,
            "messages": [
                message.to_wire()
                for message in self.messages[-PERSON_DISCUSSION_MAX_MESSAGES:]
            ],
        }


@dataclass(frozen=True)
class PersonCaseStatePayload:
    """One case as the window shows it (PERSON_CASE_CONTEXT_V1)."""

    pivot_id: int
    role: PersonCaseRole
    run_name: str
    case_title: str
    # The case's status display ("In progress", "Need help", ...).
    status_label: str
    # Absolute case page URL; blank when the server has no SITE_URL.
    case_url: str
    preconditions: str
    steps_text: str
    expected_text: str
    # Steps-template cases only; empty for free text.
    numbered_steps: tuple[PersonCaseStepPayload, ...]
    # The last whole lines of the case log.
    log_tail: str
    # CASE_PAUSE_V1 only.
    pause: PersonCasePausePayload | None = None
    # PERSON_DISCUSSION_V1 only, and only for a help case.
    help: PersonHelpPayload | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "pivot_id": self.pivot_id,
            "role": self.role.value,
            "run_name": self.run_name,
            "case_title": self.case_title,
            "status_label": self.status_label,
            "case_url": self.case_url,
            "preconditions": self.preconditions,
            "steps_text": self.steps_text,
            "expected_text": self.expected_text,
            "numbered_steps": [
                step.to_wire()
                for step in self.numbered_steps[:PERSON_CASE_NUMBERED_STEPS_MAX_ITEMS]
            ],
            "log_tail": self.log_tail,
            "pause": self.pause.to_wire() if self.pause is not None else None,
            "help": self.help.to_wire() if self.help is not None else None,
        }


@dataclass(frozen=True)
class CasePauseActionPayload:
    """A person's pause of a case from the window (``case_pause_action``)."""

    pivot_id: int
    # Optional first message for the agent; stripped.
    message: str


@dataclass(frozen=True)
class CasePauseActionResultPayload:
    ok: bool
    # CasePauseOutcome value when ok ("requested", "already_requested",
    # "paused"); "" when refused.
    outcome: str
    # User-safe text to show in the window.
    message: str


@dataclass(frozen=True)
class PersonChatSendPayload:
    """A person's discussion turn from the window (``person_chat_send``)."""

    pivot_id: int
    # The open request the window showed; a stale card is refused.
    help_request_id: int
    # Stripped, 1..PERSON_CHAT_TEXT_MAX_CHARS.
    text: str


@dataclass(frozen=True)
class PersonHelpActionPayload:
    """A person's resolution of a help request (``person_help_action``)."""

    pivot_id: int
    help_request_id: int
    action: PersonHelpActionKind
    # Stripped, ≤ PERSON_HELP_NOTE_MAX_CHARS.
    note: str
    # Continue only; None: the stored mode.
    continue_mode: ContinueModeWire | None
    # The redesign the window showed (Continue only); None when none.
    plan_revision_id: int | None
    use_plan_revision: bool


@dataclass(frozen=True)
class PersonCaseActionResultPayload:
    """The outcome of ``person_chat_send`` / ``person_help_action``."""

    ok: bool
    # User-safe text to show in the window.
    message: str


def _extract_optional_object(
    data: Mapping[str, object], field: str
) -> dict[str, object] | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtocolError(f"Invalid '{field}': expected an object")
    return value


def _extract_str_list(
    data: Mapping[str, object], field: str, max_items: int, max_chars: int
) -> tuple[str, ...]:
    raw = data.get(field, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ProtocolError(f"Invalid '{field}': expected a list of strings")
    return tuple(item[:max_chars] for item in raw[:max_items])


def _parse_continue_mode(raw: object, field: str) -> ContinueModeWire | None:
    """None for "" or absent; ProtocolError for an unknown value."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ProtocolError(f"Invalid '{field}': expected a string")
    try:
        return ContinueModeWire(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown continue mode: {raw[:40]!r}") from e


def _parse_case_pause(raw: dict[str, object]) -> PersonCasePausePayload:
    return PersonCasePausePayload(
        can_pause=_extract_bool(raw, "can_pause", default=False),
        pause_blocker=_extract_text(
            raw, "pause_blocker", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
        pause_pending=_extract_bool(raw, "pause_pending", default=False),
        pause_requested_by=_extract_text(
            raw, "pause_requested_by", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        paused=_extract_bool(raw, "paused", default=False),
        paused_by=_extract_text(
            raw, "paused_by", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        pause_message_state=_extract_text(
            raw, "pause_message_state", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        pause_message_note=_extract_text(
            raw,
            "pause_message_note",
            PERSON_STEP_RESULT_MESSAGE_MAX_CHARS,
            truncate=True,
        ),
    )


def _parse_discussion_message(raw: object) -> PersonDiscussionMessagePayload:
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid discussion message: expected an object")
    item: dict[str, object] = raw
    raw_kind = _extract_str(item, "kind")
    try:
        kind = DiscussionMessageKind(raw_kind)
    except ValueError as e:
        raise ProtocolError(f"Unknown message kind: {raw_kind[:40]!r}") from e
    return PersonDiscussionMessagePayload(
        message_id=_extract_bounded_int(item, "message_id", _PERSON_STEP_MAX_ID),
        kind=kind,
        author=_extract_text(
            item, "author", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        text=_extract_text(
            item, "text", PERSON_DISCUSSION_MESSAGE_MAX_CHARS, truncate=True
        ),
        created_at=_extract_text(
            item, "created_at", _PERSON_CASE_ISO_MAX_CHARS, truncate=True
        ),
    )


def _extract_non_negative_int(data: Mapping[str, object], field: str) -> int:
    value = data.get(field, 0)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= _PERSON_STEP_MAX_ID
    ):
        raise ProtocolError(f"Invalid '{field}': expected a non-negative int")
    return value


def _parse_plan_draft(raw: dict[str, object]) -> PersonPlanDraftPayload:
    raw_rows = raw.get("rows", [])
    if not isinstance(raw_rows, list):
        raise ProtocolError("Invalid 'rows': expected a list")
    rows: list[PersonPlanDraftRowPayload] = []
    for entry in raw_rows[:PERSON_PLAN_DRAFT_MAX_ROWS]:
        if not isinstance(entry, dict):
            raise ProtocolError("Invalid plan row: expected an object")
        row: dict[str, object] = entry
        rows.append(
            PersonPlanDraftRowPayload(
                num=_extract_bounded_int(row, "num", _PERSON_STEP_MAX_ROUND),
                description=_extract_text(
                    row, "description", PERSON_PLAN_DRAFT_TEXT_MAX_CHARS, truncate=True
                ),
                expected_result=_extract_text(
                    row,
                    "expected_result",
                    PERSON_PLAN_DRAFT_TEXT_MAX_CHARS,
                    truncate=True,
                ),
                carried_from=_extract_non_negative_int(row, "carried_from"),
                state_label=_extract_text(
                    row, "state_label", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
                ),
            )
        )
    return PersonPlanDraftPayload(
        revision_id=_extract_bounded_int(raw, "revision_id", _PERSON_STEP_MAX_ID),
        reason=_extract_text(raw, "reason", PERSON_HELP_TEXT_MAX_CHARS, truncate=True),
        resume_step=_extract_non_negative_int(raw, "resume_step"),
        carried_count=_extract_non_negative_int(raw, "carried_count"),
        rows=tuple(rows),
    )


def _parse_case_help(raw: dict[str, object]) -> PersonHelpPayload:
    stored = _parse_continue_mode(raw.get("continue_mode"), "continue_mode")
    if stored is None:
        raise ProtocolError("Missing 'continue_mode'")
    raw_choices = raw.get("continue_mode_choices", [])
    if not isinstance(raw_choices, list):
        raise ProtocolError("Invalid 'continue_mode_choices': expected a list")
    choices = tuple(
        mode
        for mode in (
            _parse_continue_mode(item, "continue_mode_choices")
            for item in raw_choices[: len(ContinueModeWire)]
        )
        if mode is not None
    )
    raw_messages = raw.get("messages", [])
    if not isinstance(raw_messages, list):
        raise ProtocolError("Invalid 'messages': expected a list")
    draft = _extract_optional_object(raw, "plan_draft")
    return PersonHelpPayload(
        help_request_id=_extract_bounded_int(
            raw, "help_request_id", _PERSON_STEP_MAX_ID
        ),
        step_num=_extract_non_negative_int(raw, "step_num"),
        reason=_extract_text(raw, "reason", PERSON_HELP_TEXT_MAX_CHARS, truncate=True),
        ask=_extract_text(raw, "ask", PERSON_HELP_TEXT_MAX_CHARS, truncate=True),
        fault_label=_extract_text(
            raw, "fault_label", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        paused_by=_extract_text(
            raw, "paused_by", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        continue_mode=stored,
        continue_mode_choices=choices,
        agent_continue_mode=_parse_continue_mode(
            raw.get("agent_continue_mode"), "agent_continue_mode"
        ),
        can_continue=_extract_bool(raw, "can_continue", default=False),
        blockers=_extract_str_list(
            raw,
            "blockers",
            PERSON_STEP_MAX_BLOCKERS,
            PERSON_STEP_RESULT_MESSAGE_MAX_CHARS,
        ),
        proposal_label=_extract_text(
            raw, "proposal_label", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        proposal_note=_extract_text(
            raw, "proposal_note", PERSON_HELP_TEXT_MAX_CHARS, truncate=True
        ),
        plan_draft=_parse_plan_draft(draft) if draft is not None else None,
        thread_url=_extract_text(
            raw, "thread_url", PERSON_STEP_URL_MAX_CHARS, truncate=True
        ),
        turn_live=_extract_bool(raw, "turn_live", default=False),
        can_send=_extract_bool(raw, "can_send", default=False),
        send_blocker=_extract_text(
            raw, "send_blocker", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
        messages=tuple(
            _parse_discussion_message(entry)
            for entry in raw_messages[-PERSON_DISCUSSION_MAX_MESSAGES:]
        ),
    )


def _parse_case(raw: object) -> PersonCaseStatePayload:
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid case: expected an object")
    item: dict[str, object] = raw
    raw_role = _extract_str(item, "role")
    try:
        role = PersonCaseRole(raw_role)
    except ValueError as e:
        raise ProtocolError(f"Unknown case role: {raw_role[:40]!r}") from e
    raw_steps = item.get("numbered_steps", [])
    if not isinstance(raw_steps, list):
        raise ProtocolError("Invalid 'numbered_steps': expected a list")
    pause = _extract_optional_object(item, "pause")
    help_state = _extract_optional_object(item, "help")
    return PersonCaseStatePayload(
        pivot_id=_extract_bounded_int(item, "pivot_id", _PERSON_STEP_MAX_ID),
        role=role,
        run_name=_extract_text(
            item, "run_name", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        case_title=_extract_text(
            item, "case_title", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        status_label=_extract_text(
            item, "status_label", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        case_url=_extract_text(
            item, "case_url", PERSON_STEP_URL_MAX_CHARS, truncate=True
        ),
        preconditions=_extract_text(
            item, "preconditions", PERSON_CASE_TEXT_MAX_CHARS, truncate=True
        ),
        steps_text=_extract_text(
            item, "steps_text", PERSON_CASE_TEXT_MAX_CHARS, truncate=True
        ),
        expected_text=_extract_text(
            item, "expected_text", PERSON_CASE_TEXT_MAX_CHARS, truncate=True
        ),
        numbered_steps=_parse_numbered_steps(raw_steps),
        log_tail=cut_log_tail(
            _extract_text(
                item, "log_tail", 4 * PERSON_CASE_LOG_TAIL_MAX_CHARS, truncate=True
            )
        ),
        pause=_parse_case_pause(pause) if pause is not None else None,
        help=_parse_case_help(help_state) if help_state is not None else None,
    )


def _parse_numbered_steps(raw: list[object]) -> tuple[PersonCaseStepPayload, ...]:
    items: list[PersonCaseStepPayload] = []
    for entry in raw[:PERSON_CASE_NUMBERED_STEPS_MAX_ITEMS]:
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


def cut_log_tail(text: str) -> str:
    """The last whole lines of ``text`` within the log tail caps (both sides).

    Keeps at most PERSON_CASE_LOG_TAIL_MAX_LINES lines and
    PERSON_CASE_LOG_TAIL_MAX_CHARS characters, newest kept; a first line cut
    by the character cap is dropped unless it is the only one.
    """
    if len(text) > PERSON_CASE_LOG_TAIL_MAX_CHARS:
        cut = text[-PERSON_CASE_LOG_TAIL_MAX_CHARS:]
        newline = cut.find("\n")
        text = cut[newline + 1 :] if 0 <= newline < len(cut) - 1 else cut
    lines = text.split("\n")
    if len(lines) > PERSON_CASE_LOG_TAIL_MAX_LINES:
        text = "\n".join(lines[-PERSON_CASE_LOG_TAIL_MAX_LINES:])
    return text


def _parse_cases(raw: object) -> tuple[PersonCaseStatePayload, ...]:
    # Absent from an older server or for a runner without the capability.
    if not isinstance(raw, list):
        raise ProtocolError("Invalid 'cases': expected a list")
    return tuple(_parse_case(entry) for entry in raw[:PERSON_CASE_STATE_MAX_CASES])


def parse_case_pause_action_payload(
    data: Mapping[str, object],
) -> CasePauseActionPayload:
    """Validate a ``case_pause_action`` (both sides); an over-long message raises."""
    return CasePauseActionPayload(
        pivot_id=_extract_bounded_int(data, "pivot_id", _PERSON_STEP_MAX_ID),
        message=_extract_text(
            data, "message", CASE_PAUSE_MESSAGE_MAX_CHARS, truncate=False
        ).strip(),
    )


def serialize_case_pause_action(action: CasePauseActionPayload, request_id: str) -> str:
    """A ``case_pause_action`` message (runner side; one request_id per pause)."""
    return serialize_message(
        MessageType.CASE_PAUSE_ACTION,
        request_id,
        pivot_id=action.pivot_id,
        message=action.message,
    )


def parse_case_pause_action_result_payload(
    data: dict[str, object],
) -> CasePauseActionResultPayload:
    """Validate a ``case_pause_action_result`` (runner side)."""
    return CasePauseActionResultPayload(
        ok=_extract_bool(data, "ok"),
        outcome=_extract_text(
            data, "outcome", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        message=_extract_text(
            data, "message", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
    )


def serialize_case_pause_action_result(
    result: CasePauseActionResultPayload, request_id: str
) -> str:
    """A ``case_pause_action_result`` (server side; echoes the pause's id)."""
    return serialize_message(
        MessageType.CASE_PAUSE_ACTION_RESULT,
        request_id,
        ok=result.ok,
        outcome=result.outcome,
        message=result.message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
    )


def parse_person_chat_send_payload(
    data: Mapping[str, object],
) -> PersonChatSendPayload:
    """Validate a ``person_chat_send`` (both sides); empty or over-long text raises."""
    text = _extract_text(
        data, "text", PERSON_CHAT_TEXT_MAX_CHARS, truncate=False
    ).strip()
    if not text:
        raise ProtocolError("Missing 'text'")
    return PersonChatSendPayload(
        pivot_id=_extract_bounded_int(data, "pivot_id", _PERSON_STEP_MAX_ID),
        help_request_id=_extract_bounded_int(
            data, "help_request_id", _PERSON_STEP_MAX_ID
        ),
        text=text,
    )


def serialize_person_chat_send(send: PersonChatSendPayload, request_id: str) -> str:
    """A ``person_chat_send`` message (runner side; one request_id per send)."""
    return serialize_message(
        MessageType.PERSON_CHAT_SEND,
        request_id,
        pivot_id=send.pivot_id,
        help_request_id=send.help_request_id,
        text=send.text,
    )


def parse_person_help_action_payload(
    data: Mapping[str, object],
) -> PersonHelpActionPayload:
    """Validate a ``person_help_action`` (both sides).

    An unknown action or mode, or an over-long note, raises ProtocolError.
    ``continue_mode``, ``plan_revision_id`` and ``use_plan_revision`` are
    dropped for actions other than ``continue``.
    """
    raw_action = data.get("action")
    if not isinstance(raw_action, str):
        raise ProtocolError("Missing or invalid 'action'")
    try:
        action = PersonHelpActionKind(raw_action)
    except ValueError as e:
        raise ProtocolError(f"Unknown help action: {raw_action[:40]!r}") from e
    note = _extract_text(
        data, "note", PERSON_HELP_NOTE_MAX_CHARS, truncate=False
    ).strip()
    continue_mode: ContinueModeWire | None = None
    plan_revision_id: int | None = None
    use_plan_revision = False
    if action is PersonHelpActionKind.CONTINUE:
        continue_mode = _parse_continue_mode(data.get("continue_mode"), "continue_mode")
        raw_revision = _extract_non_negative_int(data, "plan_revision_id")
        plan_revision_id = raw_revision or None
        use_plan_revision = _extract_bool(
            dict(data), "use_plan_revision", default=False
        )
    return PersonHelpActionPayload(
        pivot_id=_extract_bounded_int(data, "pivot_id", _PERSON_STEP_MAX_ID),
        help_request_id=_extract_bounded_int(
            data, "help_request_id", _PERSON_STEP_MAX_ID
        ),
        action=action,
        note=note,
        continue_mode=continue_mode,
        plan_revision_id=plan_revision_id,
        use_plan_revision=use_plan_revision,
    )


def serialize_person_help_action(
    action: PersonHelpActionPayload, request_id: str
) -> str:
    """A ``person_help_action`` message (runner side; one request_id per action)."""
    return serialize_message(
        MessageType.PERSON_HELP_ACTION,
        request_id,
        pivot_id=action.pivot_id,
        help_request_id=action.help_request_id,
        action=action.action.value,
        note=action.note,
        continue_mode=(
            action.continue_mode.value if action.continue_mode is not None else ""
        ),
        plan_revision_id=action.plan_revision_id or 0,
        use_plan_revision=action.use_plan_revision,
    )


def parse_person_case_action_result_payload(
    data: dict[str, object],
) -> PersonCaseActionResultPayload:
    """Validate a ``person_case_action_result`` (runner side)."""
    return PersonCaseActionResultPayload(
        ok=_extract_bool(data, "ok"),
        message=_extract_text(
            data, "message", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
    )


def serialize_person_case_action_result(
    result: PersonCaseActionResultPayload, request_id: str
) -> str:
    """A ``person_case_action_result`` (server side; echoes the request's id)."""
    return serialize_message(
        MessageType.PERSON_CASE_ACTION_RESULT,
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


# --- Artifact scan (ARTIFACT_SCAN_V1) ---------------------------------------
#
# Read-only inventory of the test machine's file system. ``scan_directory``
# lists a bounded tree (``controller_client/artifact_scan.py``);
# ``inspect_paths`` stats a few absolute paths. Neither reads file contents nor
# runs a shell. Both are answered with one ``artifact_scan_result`` message
# whose ``result`` is the JSON form below; the server re-validates it.

# Runner-side hard caps; the server's settings must not exceed them.
SCAN_DIRECTORY_MAX_DEPTH: Final[int] = 4
SCAN_DIRECTORY_MAX_ENTRIES: Final[int] = 2000
# Wall-clock budget of one scan on the runner; the result is then truncated.
SCAN_DIRECTORY_TIME_LIMIT_SECONDS: Final[float] = 10.0
INSPECT_PATHS_MAX: Final[int] = 50
# Longest absolute path a request may carry.
ARTIFACT_PATH_MAX_CHARS: Final[int] = 4096
# Longest relative entry path a scan reports; deeper names are skipped.
SCAN_ENTRY_PATH_MAX_CHARS: Final[int] = 1024
# Longest ``error`` text a scan result carries.
SCAN_ERROR_MAX_CHARS: Final[int] = 500


@dataclass(frozen=True)
class ScanDirectoryPayload:
    """``scan_directory``: an absolute path (``~`` expanded on the runner)."""

    path: str
    # 1..SCAN_DIRECTORY_MAX_DEPTH; 1 lists only the directory's own children.
    max_depth: int
    # 1..SCAN_DIRECTORY_MAX_ENTRIES.
    max_entries: int
    # A network root (``is_network_path``) is refused unless this is True.
    # The server sets it only for a base directory a person entered.
    allow_network: bool = False


@dataclass(frozen=True)
class InspectPathsPayload:
    """``inspect_paths``: 1..INSPECT_PATHS_MAX absolute paths."""

    paths: tuple[str, ...]
    # Network paths (``is_network_path``) come back absent unless this is
    # True; the server sets it only for paths under a person-entered base
    # directory.
    allow_network: bool = False


_PATH_SEPARATORS = "\\/"


def app_check_variants(query: str) -> list[str]:
    """The names ``check_app_installed`` looks up for ``query``, in order: as
    given, lower-cased with spaces as ``-``, then without ``-``."""
    base = query.lower().replace(" ", "-")
    variants: list[str] = [query]
    if base != query:
        variants.append(base)
    no_separator = base.replace("-", "")
    if no_separator not in variants:
        variants.append(no_separator)
    return variants


def is_network_app_target(query: str) -> bool:
    """Whether any lookup variant of an app check target is a network path
    (``\\-\\host\\s`` turns into ``\\\\host\\s`` once ``-`` is removed)."""
    return any(is_network_path(variant) for variant in app_check_variants(query))


def is_network_path(path: str) -> bool:
    """Whether ``path`` may name another machine: ``\\\\host\\share``, ``//host``.

    On Windows merely opening such a path makes an SMB connection that can
    send the runner user's NTLM credentials to that host, so neither scan
    action touches one unless the server explicitly allows it. Mixed
    separators and the ``\\\\?\\`` / ``\\\\.\\`` device prefixes count too, and
    so does the NT object namespace prefix ``\\??\\`` (``\\??\\UNC\\host\\share``)
    in any separator mix: such paths are refused whole, even ``\\??\\C:\\``.
    """
    head = path.strip()[:4]
    if len(head) >= 2 and head[0] in _PATH_SEPARATORS and head[1] in _PATH_SEPARATORS:
        return True
    return (
        len(head) == 4
        and head[0] in _PATH_SEPARATORS
        and head[1:3] == "??"
        and head[3] in _PATH_SEPARATORS
    )


@dataclass(frozen=True)
class ScanEntryPayload:
    """One scanned path; field names are kept short on the wire.

    ``p`` path relative to the scan root with "/" separators, ``d`` is a
    directory, ``s`` size in bytes (0 for directories), ``m`` mtime as epoch
    seconds.
    """

    p: str
    d: bool
    s: int
    m: float


@dataclass(frozen=True)
class ScanDirectoryResultPayload:
    # The expanded, normalized absolute path that was scanned (or requested,
    # when the scan was refused).
    root: str
    # Breadth first: shallower entries come first when the list is truncated.
    entries: tuple[ScanEntryPayload, ...]
    # True when ``max_entries`` or the time limit cut the listing short.
    truncated: bool
    # Directories or entries that could not be read for lack of permission.
    permission_errors: int
    # Why nothing was scanned (not absolute, missing, not a directory, a link,
    # unreadable); "" on success.
    error: str


@dataclass(frozen=True)
class PathInfoPayload:
    # The path exactly as requested.
    path: str
    exists: bool
    is_dir: bool
    # Bytes; 0 for directories and missing paths.
    size: int
    # Epoch seconds; 0.0 for missing paths.
    mtime: float


def _extract_path(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"Missing or invalid '{field}': expected a path")
    if len(value) > ARTIFACT_PATH_MAX_CHARS or "\x00" in value:
        raise ProtocolError(f"Invalid '{field}': path too long or contains NUL")
    return value


def parse_scan_directory_payload(data: dict[str, object]) -> ScanDirectoryPayload:
    return ScanDirectoryPayload(
        path=_extract_path(data, "path"),
        max_depth=_extract_bounded_int(data, "max_depth", SCAN_DIRECTORY_MAX_DEPTH),
        max_entries=_extract_bounded_int(
            data, "max_entries", SCAN_DIRECTORY_MAX_ENTRIES
        ),
        allow_network=_extract_bool(data, "allow_network", default=False),
    )


def parse_inspect_paths_payload(data: dict[str, object]) -> InspectPathsPayload:
    raw = data.get("paths")
    if not isinstance(raw, list) or not 1 <= len(raw) <= INSPECT_PATHS_MAX:
        raise ProtocolError(
            f"Missing or invalid 'paths': expected 1 to {INSPECT_PATHS_MAX} paths"
        )
    return InspectPathsPayload(
        paths=tuple(_extract_path({"path": item}, "path") for item in raw),
        allow_network=_extract_bool(data, "allow_network", default=False),
    )


def scan_directory_result_to_json(
    result: ScanDirectoryResultPayload,
) -> dict[str, object]:
    return {
        "root": result.root,
        "entries": [
            {"p": entry.p, "d": entry.d, "s": entry.s, "m": entry.m}
            for entry in result.entries
        ],
        "truncated": result.truncated,
        "permission_errors": result.permission_errors,
        "error": result.error,
    }


def inspect_paths_result_to_json(
    result: Sequence[PathInfoPayload],
) -> list[dict[str, object]]:
    return [
        {
            "path": info.path,
            "exists": info.exists,
            "is_dir": info.is_dir,
            "size": info.size,
            "mtime": info.mtime,
        }
        for info in result
    ]


def _extract_size(data: Mapping[str, object], field: str) -> int:
    value = data.get(field, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError(f"Invalid '{field}': expected a non-negative int")
    return value


def _extract_mtime(data: Mapping[str, object], field: str) -> float:
    value = data.get(field, 0.0)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ProtocolError(f"Invalid '{field}': expected a number")
    return float(value)


def _parse_scan_entry(raw: object) -> ScanEntryPayload:
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid scan entry: expected an object")
    item: dict[str, object] = raw
    path = item.get("p")
    if (
        not isinstance(path, str)
        or not path
        or len(path) > SCAN_ENTRY_PATH_MAX_CHARS
        or "\\" in path
        or path.startswith("/")
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise ProtocolError("Invalid scan entry path: expected a relative path")
    return ScanEntryPayload(
        p=path,
        d=_extract_bool(item, "d"),
        s=_extract_size(item, "s"),
        m=_extract_mtime(item, "m"),
    )


def parse_scan_directory_result(
    raw: object, *, max_entries: int
) -> ScanDirectoryResultPayload:
    """Validate a ``scan_directory`` result (server side).

    Entries must be relative paths below the root, at most ``max_entries`` of
    them; anything else raises ProtocolError.
    """
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid scan result: expected an object")
    data: dict[str, object] = raw
    entries = data.get("entries", [])
    if not isinstance(entries, list) or len(entries) > max_entries:
        raise ProtocolError(
            f"Invalid 'entries': expected a list of at most {max_entries} entries"
        )
    root = data.get("root", "")
    if not isinstance(root, str) or len(root) > ARTIFACT_PATH_MAX_CHARS:
        raise ProtocolError("Invalid 'root': expected a path")
    return ScanDirectoryResultPayload(
        root=root,
        entries=tuple(_parse_scan_entry(entry) for entry in entries),
        truncated=_extract_bool(data, "truncated", default=False),
        permission_errors=_extract_size(data, "permission_errors"),
        error=_extract_text(data, "error", SCAN_ERROR_MAX_CHARS, truncate=True),
    )


def parse_inspect_paths_result(
    raw: object, *, paths: Sequence[str]
) -> tuple[PathInfoPayload, ...]:
    """Validate an ``inspect_paths`` result (server side).

    One item per requested path, in the same order and echoing it.
    """
    if not isinstance(raw, list) or len(raw) != len(paths):
        raise ProtocolError(
            f"Invalid inspect result: expected {len(paths)} items in a list"
        )
    infos: list[PathInfoPayload] = []
    for requested, item in zip(paths, raw, strict=True):
        if not isinstance(item, dict) or item.get("path") != requested:
            raise ProtocolError("Invalid inspect result: items do not match paths")
        fields: dict[str, object] = item
        exists = _extract_bool(fields, "exists")
        infos.append(
            PathInfoPayload(
                path=requested,
                exists=exists,
                is_dir=exists and _extract_bool(fields, "is_dir", default=False),
                size=_extract_size(fields, "size"),
                mtime=_extract_mtime(fields, "mtime"),
            )
        )
    return tuple(infos)
