import base64
import binascii
import hashlib
import json
import re
import unicodedata
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
CLIENT_VERSION: Final[str] = "0.23.3"


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
    # Retired in 0.18.0: ``person_step_question`` / ``person_step_question_result``
    # (Ask AI) may still arrive from runners < 0.18.0; ignored; never reuse.
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
    # Manual run (MANUAL_RUN_V1): server -> runner with a snapshot of the
    # manual run; runner -> server with a claim or release of a case, a case
    # hand-in and one screenshot of it; server -> runner with each outcome
    # (same request_id). New state arrives in the next ``manual_run_state``.
    MANUAL_RUN_STATE = "manual_run_state"
    MANUAL_CASE_ACTION = "manual_case_action"
    MANUAL_CASE_ACTION_RESULT = "manual_case_action_result"
    MANUAL_CASE_SUBMIT = "manual_case_submit"
    MANUAL_CASE_SUBMIT_RESULT = "manual_case_submit_result"
    MANUAL_SCREENSHOT_UPLOAD = "manual_screenshot_upload"
    MANUAL_SCREENSHOT_ACK = "manual_screenshot_ack"
    # Manual pause and discuss (MANUAL_PAUSE_DISCUSS_V1): runner -> server
    # with the tester's step marks so far (no reply) and a discussion turn
    # about the claimed case (answered by ``manual_case_action_result``).
    MANUAL_CASE_PROGRESS = "manual_case_progress"
    MANUAL_CHAT_SEND = "manual_chat_send"
    # Phone or tablet target (MOBILE_DEVICE_V1): server -> runner gestures and
    # app actions, answered by ``action_result`` / ``error``; runner -> server
    # ``device_status`` (no reply) on every change and every
    # DEVICE_STATUS_INTERVAL_SECONDS.
    SWIPE = "swipe"
    LONG_PRESS = "long_press"
    PRESS_BUTTON = "press_button"
    ROTATE_DEVICE = "rotate_device"
    HIDE_KEYBOARD = "hide_keyboard"
    OPEN_URL = "open_url"
    INSTALL_APP = "install_app"
    DEVICE_STATUS = "device_status"
    # Window capture (WINDOW_CAPTURE_V1): server -> runner with
    # ``list_windows`` or ``capture_window``; runner -> server with
    # ``window_list_result`` / ``window_capture_result`` (same request_id)
    # carrying the result object under ``result``, as ``artifact_scan_result``.
    LIST_WINDOWS = "list_windows"
    CAPTURE_WINDOW = "capture_window"
    WINDOW_LIST_RESULT = "window_list_result"
    WINDOW_CAPTURE_RESULT = "window_capture_result"
    # The installed apps of the phone (DEVICE_APPS_V1): server -> runner with
    # ``list_device_apps`` (no fields besides ``request_id``); runner -> server
    # with ``device_apps_result`` (same request_id) carrying the result object
    # under ``result``, as ``window_list_result``.
    LIST_DEVICE_APPS = "list_device_apps"
    DEVICE_APPS_RESULT = "device_apps_result"
    # Kept sessions (KEPT_SESSIONS_V1): server -> runner with
    # ``list_interactive_sessions`` (no fields besides ``request_id``);
    # runner -> server with ``interactive_sessions`` (same request_id)
    # carrying ``sessions``. Runner-local: never resent after a reconnect.
    LIST_INTERACTIVE_SESSIONS = "list_interactive_sessions"
    INTERACTIVE_SESSIONS = "interactive_sessions"
    # Web browser layer (WEB_BROWSER_V1, WebDriver based; separate from the
    # Playwright ``browser_*`` messages): server -> runner with one of the
    # ``web_browser_*`` requests; runner -> server with ``web_browser_result``
    # (same request_id) or an ``error``.
    WEB_BROWSER_OPEN = "web_browser_open"
    WEB_BROWSER_LIST = "web_browser_list"
    WEB_BROWSER_NAVIGATE = "web_browser_navigate"
    WEB_BROWSER_HISTORY = "web_browser_history"
    WEB_BROWSER_TABS = "web_browser_tabs"
    WEB_BROWSER_ELEMENTS = "web_browser_elements"
    WEB_BROWSER_ACT = "web_browser_act"
    WEB_BROWSER_READ = "web_browser_read"
    WEB_BROWSER_WAIT = "web_browser_wait"
    WEB_BROWSER_EVALUATE = "web_browser_evaluate"
    WEB_BROWSER_CLOSE = "web_browser_close"
    WEB_BROWSER_RESULT = "web_browser_result"


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
    # Optional: Manual runs in the window (``manual_run_state``,
    # ``manual_case_action``, ``manual_case_submit``,
    # ``manual_screenshot_upload`` and their results). Advertised only
    # together with PERSON_STEPS_V1.
    MANUAL_RUN_V1 = "manual_run_v1"
    # Optional: Pause / Continue of the claimed manual case
    # (``manual_case_action`` ``pause`` / ``continue``), the tester's marks so
    # far (``manual_case_progress``), the case's discussion (``active.discussion``,
    # ``manual_chat_send``) and the pause fields of ``active``. Advertised only
    # together with MANUAL_RUN_V1 (dropped with the window).
    MANUAL_PAUSE_DISCUSS_V1 = "manual_pause_discuss_v1"
    # Optional: the runner drives a phone or tablet (``system_info.target`` is
    # ``android`` or ``ios``), answers the phone messages above and reports
    # ``device_status``. Advertised only on a phone target.
    MOBILE_DEVICE_V1 = "mobile_device_v1"
    # Optional: ``list_windows`` and ``capture_window`` (allowlisted window,
    # region and screen capture, answered with ``window_list_result`` /
    # ``window_capture_result``). Every build from 0.20.0 advertises it on
    # every OS and target, with or without the person window, so a runner
    # without it is outdated; a platform that cannot capture says so in the
    # result's ``status``.
    WINDOW_CAPTURE_V1 = "window_capture_v1"
    # Optional: kept interactive sessions. ``start_interactive_cmd`` may carry
    # ``keep``, ``max_seconds``, ``initial_read_ms`` and ``max_kept``; a kept
    # session is never replaced by a later start, its output is buffered
    # (``output_dropped`` on ``interactive_output``) and it ends after
    # ``max_seconds``; ``list_interactive_sessions`` lists the sessions. A
    # kept start over ``max_kept`` is answered with TOO_MANY_SESSIONS. Older
    # controllers ignore the fields and keep one session.
    KEPT_SESSIONS_V1 = "kept_sessions_v1"
    # Optional: the ``web_browser_*`` messages (WebDriver browser layer for
    # Chrome, Edge, Firefox and Safari on desktop and phones), answered with
    # ``web_browser_result``. Every build from 0.22.0 advertises it on every
    # OS and target; which browsers a runner serves is
    # ``web_browsers_for(target, host_os)``.
    WEB_BROWSER_V1 = "web_browser_v1"
    # Optional: ``list_device_apps`` (the installed apps of the phone, answered
    # with ``device_apps_result``). Advertised only on a phone target, together
    # with MOBILE_DEVICE_V1; a driver that cannot list apps says so in the
    # result's ``status``.
    DEVICE_APPS_V1 = "device_apps_v1"
    # Retired in 0.18.0 (How to do it and Ask AI): person_step_assist_v1,
    # person_step_guide_v1, manual_step_guide_v1 may still arrive from runners
    # < 0.18.0; ignored; never reuse.


# Capabilities that need the person step window; dropped without it.
_PERSON_WINDOW_CAPABILITIES: Final[frozenset[ClientCapability]] = frozenset(
    {
        ClientCapability.PERSON_STEPS_V1,
        ClientCapability.PERSON_STEP_CASE_STEPS_V1,
        ClientCapability.PERSON_CASE_CONTEXT_V1,
        ClientCapability.PERSON_DISCUSSION_V1,
        ClientCapability.CASE_PAUSE_V1,
        ClientCapability.MANUAL_RUN_V1,
        ClientCapability.MANUAL_PAUSE_DISCUSS_V1,
    }
)


# Capabilities that need a phone target; dropped on a desktop runner.
_MOBILE_CAPABILITIES: Final[frozenset[ClientCapability]] = frozenset(
    {
        ClientCapability.MOBILE_DEVICE_V1,
        ClientCapability.DEVICE_APPS_V1,
    }
)


# Everything this client build knows. The handshake sends
# ``advertised_capabilities(...)``, which may drop the runtime-dependent ones.
CLIENT_CAPABILITIES: Final[tuple[ClientCapability, ...]] = tuple(ClientCapability)


def advertised_capabilities(
    person_window: bool, mobile: bool = False
) -> tuple[ClientCapability, ...]:
    """The capabilities to send in the handshake.

    ``person_window`` is False when the person step window is disabled
    (``CONTROLLER_PERSON_WINDOW=false``) or Tk is unavailable; the server then
    never sends ``person_step_state`` and the person answers on the web.
    PERSON_STEP_CASE_STEPS_V1, PERSON_CASE_CONTEXT_V1, PERSON_DISCUSSION_V1,
    CASE_PAUSE_V1, MANUAL_RUN_V1 and MANUAL_PAUSE_DISCUSS_V1 are dropped with
    it. ``mobile`` is True only on a phone target; MOBILE_DEVICE_V1 and
    DEVICE_APPS_V1 are dropped otherwise.
    """
    return tuple(
        capability
        for capability in CLIENT_CAPABILITIES
        if (person_window or capability not in _PERSON_WINDOW_CAPABILITIES)
        and (mobile or capability not in _MOBILE_CAPABILITIES)
    )


# What the server insists on; a controller missing any of them would
# authenticate fine and then silently time out on first use. Optional
# capabilities (BROWSER_SELECTION_V1, DOUBLE_CLICK_V1, CLOSE_APP_V1,
# WINDOWS_SHELL_V1, SCROLL_V1, SELF_UPDATE_V1, PERSON_STEPS_V1,
# PERSON_STEP_CASE_STEPS_V1, ARTIFACT_SCAN_V1, PERSON_CASE_CONTEXT_V1,
# PERSON_DISCUSSION_V1, CASE_PAUSE_V1, MANUAL_RUN_V1, MANUAL_PAUSE_DISCUSS_V1,
# MOBILE_DEVICE_V1, WINDOW_CAPTURE_V1, KEPT_SESSIONS_V1, WEB_BROWSER_V1,
# DEVICE_APPS_V1) are checked where they are used instead.
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
    # The phone or tablet the runner drives is gone (unplugged, emulator
    # stopped, Appium session lost) or still starting.
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    # A desktop-only message on a phone target, or a phone message on a
    # desktop target.
    TARGET_MISMATCH = "TARGET_MISMATCH"
    # A kept ``start_interactive_cmd`` while ``max_kept`` kept sessions are
    # already running (KEPT_SESSIONS_V1).
    TOO_MANY_SESSIONS = "TOO_MANY_SESSIONS"
    # WEB_BROWSER_V1. ``details`` of SETUP_NEEDED is one WebBrowserSetupStep
    # value; of NOT_OPEN the named browser (or empty); of AMBIGUOUS the open
    # browsers, comma-separated WebBrowser values. SCRIPT_FAILED carries the
    # JavaScript error class only.
    WEB_BROWSER_SETUP_NEEDED = "WEB_BROWSER_SETUP_NEEDED"
    WEB_BROWSER_UNSUPPORTED = "WEB_BROWSER_UNSUPPORTED"
    WEB_BROWSER_NOT_OPEN = "WEB_BROWSER_NOT_OPEN"
    WEB_BROWSER_AMBIGUOUS = "WEB_BROWSER_AMBIGUOUS"
    SCRIPT_FAILED = "SCRIPT_FAILED"


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


# Kept interactive sessions (KEPT_SESSIONS_V1). The parser clamps the
# request's values to these limits.
KEPT_SESSIONS_MAX: Final[int] = 4
KEPT_SESSION_MAX_SECONDS: Final[int] = 14_400
# Output a kept session buffers between reads; older output is dropped.
KEPT_SESSION_BUFFER_BYTES: Final[int] = 1_048_576
# Bounds of the first read after a start, in milliseconds.
INITIAL_READ_MS_MIN: Final[int] = 200
INITIAL_READ_MS_MAX: Final[int] = 5_000
# Most sessions one ``interactive_sessions`` reply carries.
INTERACTIVE_SESSIONS_MAX_ENTRIES: Final[int] = 32
# Longest session id an ``interactive_sessions`` reply may carry.
INTERACTIVE_SESSION_ID_MAX_CHARS: Final[int] = 128


@dataclass(frozen=True)
class StartInteractiveCmdPayload:
    command: str
    # KEPT_SESSIONS_V1 only; absent from older servers. A kept session needs
    # ``max_seconds`` and ``max_kept``.
    keep: bool = False
    max_seconds: int | None = None
    initial_read_ms: int | None = None
    max_kept: int | None = None


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
    # KEPT_SESSIONS_V1: True when a kept session's buffer discarded output
    # since the last read. Older servers ignore it.
    output_dropped: bool = False


@dataclass(frozen=True)
class InteractiveSessionInfo:
    session_id: str
    alive: bool
    exit_code: int | None
    elapsed_ms: float
    kept: bool


@dataclass(frozen=True)
class InteractiveSessionsPayload:
    sessions: tuple[InteractiveSessionInfo, ...]


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


def _clamped_optional_int(
    data: dict[str, object], field: str, minimum: int, maximum: int
) -> int | None:
    value = _extract_optional_int(data, field)
    if value is None:
        return None
    return min(max(value, minimum), maximum)


def parse_start_interactive_cmd_payload(
    data: dict[str, object],
) -> StartInteractiveCmdPayload:
    """Validate a ``start_interactive_cmd`` request (runner side).

    The KEPT_SESSIONS_V1 fields may be absent (older servers); present values
    are clamped to the limits. ``keep`` without ``max_seconds`` and
    ``max_kept`` is refused."""
    keep = _extract_bool(data, "keep", default=False)
    max_seconds = _clamped_optional_int(
        data, "max_seconds", 1, KEPT_SESSION_MAX_SECONDS
    )
    max_kept = _clamped_optional_int(data, "max_kept", 1, KEPT_SESSIONS_MAX)
    if keep and (max_seconds is None or max_kept is None):
        raise ProtocolError("A kept session needs 'max_seconds' and 'max_kept'")
    return StartInteractiveCmdPayload(
        command=_extract_str(data, "command"),
        keep=keep,
        max_seconds=max_seconds,
        initial_read_ms=_clamped_optional_int(
            data, "initial_read_ms", INITIAL_READ_MS_MIN, INITIAL_READ_MS_MAX
        ),
        max_kept=max_kept,
    )


def serialize_start_interactive_cmd_payload(
    payload: StartInteractiveCmdPayload,
) -> dict[str, object]:
    """The JSON fields of a ``start_interactive_cmd`` request (server side;
    passed as the action's keyword payload). The KEPT_SESSIONS_V1 fields are
    sent only when set, so a plain start is unchanged."""
    fields: dict[str, object] = {"command": payload.command}
    if payload.keep:
        fields["keep"] = True
    if payload.max_seconds is not None:
        fields["max_seconds"] = payload.max_seconds
    if payload.initial_read_ms is not None:
        fields["initial_read_ms"] = payload.initial_read_ms
    if payload.max_kept is not None:
        fields["max_kept"] = payload.max_kept
    return fields


def serialize_list_interactive_sessions_payload() -> dict[str, object]:
    """The JSON fields of a ``list_interactive_sessions`` request (server
    side): none besides ``request_id``."""
    return {}


def serialize_interactive_sessions_payload(
    payload: InteractiveSessionsPayload,
) -> dict[str, object]:
    """The JSON fields of an ``interactive_sessions`` reply (runner side),
    cut to INTERACTIVE_SESSIONS_MAX_ENTRIES sessions."""
    return {
        "sessions": [
            {
                "session_id": info.session_id,
                "alive": info.alive,
                "exit_code": info.exit_code,
                "elapsed_ms": max(info.elapsed_ms, 0.0),
                "kept": info.kept,
            }
            for info in payload.sessions[:INTERACTIVE_SESSIONS_MAX_ENTRIES]
        ]
    }


def serialize_interactive_sessions_message(
    payload: InteractiveSessionsPayload, request_id: str
) -> str:
    """An ``interactive_sessions`` message (runner side; echoes the
    request's ``request_id``)."""
    return serialize_message(
        MessageType.INTERACTIVE_SESSIONS,
        request_id,
        **serialize_interactive_sessions_payload(payload),
    )


def _parse_interactive_session_info(raw: object) -> InteractiveSessionInfo:
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid session: expected an object")
    session_id = _extract_str(raw, "session_id")
    if (
        not session_id
        or len(session_id) > INTERACTIVE_SESSION_ID_MAX_CHARS
        or not session_id.isprintable()
    ):
        raise ProtocolError("Invalid 'session_id'")
    elapsed_ms = _extract_number(raw, "elapsed_ms", default=0.0)
    if elapsed_ms < 0:
        raise ProtocolError("Invalid 'elapsed_ms': expected a non-negative number")
    return InteractiveSessionInfo(
        session_id=session_id,
        alive=_extract_bool(raw, "alive"),
        exit_code=_extract_optional_int(raw, "exit_code"),
        elapsed_ms=elapsed_ms,
        kept=_extract_bool(raw, "kept", default=False),
    )


def parse_interactive_sessions_payload(
    data: dict[str, object],
) -> InteractiveSessionsPayload:
    """Validate an ``interactive_sessions`` reply (server side): at most
    INTERACTIVE_SESSIONS_MAX_ENTRIES sessions. Raises ProtocolError."""
    raw = data.get("sessions", [])
    if not isinstance(raw, list):
        raise ProtocolError("Invalid 'sessions': expected a list")
    if len(raw) > INTERACTIVE_SESSIONS_MAX_ENTRIES:
        raise ProtocolError(
            "Invalid 'sessions': expected at most "
            f"{INTERACTIVE_SESSIONS_MAX_ENTRIES} sessions"
        )
    return InteractiveSessionsPayload(
        sessions=tuple(_parse_interactive_session_info(item) for item in raw)
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
# Test case steps a person step covers (PERSON_STEP_CASE_STEPS_V1); the
# server's PERSON_STEP_CASE_STEPS_* caps in projects.models. Cut when parsed.
PERSON_STEP_CASE_STEPS_MAX_ITEMS: Final[int] = 15
PERSON_STEP_CASE_STEP_TEXT_MAX_CHARS: Final[int] = 1000
# Any positive step number the server stored; only the type is checked.
_PERSON_STEP_MAX_CASE_STEP_NUM: Final[int] = 2**31 - 1
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
        cases=_parse_cases(data.get("cases", [])),
    )


def serialize_person_step_state(
    steps: Sequence[PersonStepStatePayload],
    *,
    cases: Sequence["PersonCaseStatePayload"] | None = None,
) -> str:
    """A ``person_step_state`` message (server side; fresh request_id).

    ``cases`` only for a runner with PERSON_CASE_CONTEXT_V1 (None: the key is
    left out, as an older server does).
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
    # At the cap the text may be the SQL tail of a longer log: its first
    # line is then cut mid-way and is dropped.
    if len(text) >= PERSON_CASE_LOG_TAIL_MAX_CHARS:
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


# ---------------------------------------------------------------------------
# Manual run (MANUAL_RUN_V1)
# ---------------------------------------------------------------------------

# Largest screenshot (raw PNG or JPEG bytes) one ``manual_screenshot_upload``
# carries. The server setting MANUAL_SHOT_MAX_BYTES may only lower it.
MANUAL_SHOT_MAX_BYTES: Final[int] = 7 * _MIB
# Largest screenshot in pixels (one 8K screen). The runner downscales to it
# before encoding; the server refuses anything larger (decode memory guard).
MANUAL_SHOT_MAX_PIXELS: Final[int] = 7680 * 4320
# Room for the rest of a ``manual_screenshot_upload`` besides the base64 text.
_MANUAL_UPLOAD_JSON_OVERHEAD_BYTES: Final[int] = 4 * 1024
# Largest raw ``manual_screenshot_upload`` message the server reads.
MANUAL_SCREENSHOT_UPLOAD_MAX_BYTES: Final[int] = (
    4 * -(-MANUAL_SHOT_MAX_BYTES // 3) + _MANUAL_UPLOAD_JSON_OVERHEAD_BYTES
)
assert (
    MANUAL_SCREENSHOT_UPLOAD_MAX_BYTES <= MAX_MESSAGE_SIZE
), "MANUAL_SHOT_MAX_BYTES does not fit in MAX_MESSAGE_SIZE"
# Verbatim steps of one manual case (the server drops steps beyond it).
MANUAL_CASE_MAX_STEPS: Final[int] = 100
# Screenshots of one hand-in: up to MANUAL_STEP_MAX_SHOTS per marked step
# plus one optional case screenshot (D8). A runner sends more than one per
# step only when the server's snapshot allows it (``step_max_shots``).
MANUAL_STEP_MAX_SHOTS: Final[int] = 5
MANUAL_CASE_SHOT_MAX: Final[int] = 1
MANUAL_SUBMISSION_MAX_SHOTS: Final[int] = (
    MANUAL_CASE_MAX_STEPS * MANUAL_STEP_MAX_SHOTS + MANUAL_CASE_SHOT_MAX
)
# The tester's notes; refused (not cut) when longer. The server stores them
# within these limits (projects.models imports them).
MANUAL_STEP_NOTE_MAX_CHARS: Final[int] = 2000
MANUAL_VERDICT_NOTE_MAX_CHARS: Final[int] = 4000
# A step's time on the window's timer; longer values are capped, not refused.
MANUAL_STEP_MAX_SECONDS: Final[int] = 24 * 3600
# Largest raw ``manual_case_submit`` message the server reads: room for every
# step with a full note (JSON may escape a character as six bytes) besides
# 64 KiB for the rest.
MANUAL_CASE_SUBMIT_MAX_BYTES: Final[int] = 64 * 1024 + MANUAL_CASE_MAX_STEPS * (
    16 * 1024
)
# Largest raw ``manual_case_action`` message the server reads.
MANUAL_CASE_ACTION_MAX_BYTES: Final[int] = 4 * 1024
# Largest raw ``manual_case_progress`` message the server reads (every step
# with a full note, as for the hand-in).
MANUAL_CASE_PROGRESS_MAX_BYTES: Final[int] = MANUAL_CASE_SUBMIT_MAX_BYTES
# Largest raw ``manual_chat_send`` message the server reads.
MANUAL_CHAT_SEND_MAX_BYTES: Final[int] = 64 * 1024
# Server-minted claim token (``TestRunTestCase.manual_claim_token``).
MANUAL_CLAIM_TOKEN_MAX_CHARS: Final[int] = 32
_MANUAL_CLAIM_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,32}")
# Runner-minted screenshot key, unique within its hand-in; the server's
# idempotency key (``TestRunScreenshot.manual_key``). Short enough that
# "m" + key fits ``SubTaskAttempt.attempt_id`` (32 characters).
MANUAL_SHOT_KEY_MAX_CHARS: Final[int] = 24
_MANUAL_SHOT_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,24}")
# Per step: a fully escaped note (six bytes a character), its screenshot keys
# in ``steps`` and their ``shots`` manifest entries (about 160 bytes each) fit
# the 16 KiB a step is given.
assert (
    MANUAL_STEP_NOTE_MAX_CHARS * 6
    + MANUAL_STEP_MAX_SHOTS * (MANUAL_SHOT_KEY_MAX_CHARS + 4 + 160)
    <= 16 * 1024
), "A step's note and screenshots no longer fit MANUAL_CASE_SUBMIT_MAX_BYTES"
# Snapshot caps; longer lists are cut, longer texts cut when parsed.
MANUAL_RUN_STATE_MAX_CASES: Final[int] = 300
MANUAL_PENDING_UPLOADS_MAX: Final[int] = 50
MANUAL_STEP_TEXT_MAX_CHARS: Final[int] = 8000
MANUAL_PRECONDITIONS_MAX_CHARS: Final[int] = 8000
MANUAL_CASE_TITLE_MAX_CHARS: Final[int] = 500
MANUAL_TESTRAIL_ID_MAX_CHARS: Final[int] = 50
MANUAL_CASE_STATUS_MAX_CHARS: Final[int] = 32
_MANUAL_ISO_MAX_CHARS: Final[int] = 64
# Bytes a character takes in the estimate below: any BMP character in the
# window's UTF-8 IPC line (JSON escapes are rare in case text).
MANUAL_RUN_STATE_BYTES_PER_CHAR: Final[int] = 3
# Assumed worst case of one ``manual_run_state`` before its discussion, in
# text lengths times MANUAL_RUN_STATE_BYTES_PER_CHAR (the server does not
# measure it). The server's ensure_ascii JSON writes a BMP non-ASCII character
# as six bytes (twice the estimate), which must still fit the WebSocket cap;
# characters outside the BMP (twelve bytes) are rare enough in case text to
# leave out of the bound. The window's IPC cap is checked against it in
# person_window_ipc.
MANUAL_RUN_STATE_SAFE_BYTES: Final[int] = 3 * _MIB
assert (
    2 * MANUAL_RUN_STATE_SAFE_BYTES <= MAX_MESSAGE_SIZE
), "MANUAL_RUN_STATE_SAFE_BYTES does not fit in MAX_MESSAGE_SIZE"
# The claimed case's discussion (``active.discussion``,
# MANUAL_PAUSE_DISCUSS_V1) is added after that budget, so its worst case (every
# row at its caps, plus the JSON keys, the URL and the blocker) must fit on
# top of it: in the server's ensure_ascii JSON (twice the estimate, as above)
# and in the window's IPC line (person_window_ipc).
_MANUAL_DISCUSSION_ROW_JSON_OVERHEAD_CHARS: Final[int] = 96
_MANUAL_DISCUSSION_JSON_OVERHEAD_CHARS: Final[int] = 128
MANUAL_DISCUSSION_SNAPSHOT_MAX_BYTES: Final[int] = (
    PERSON_DISCUSSION_MAX_MESSAGES
    * (
        PERSON_DISCUSSION_MESSAGE_MAX_CHARS
        + PERSON_STEP_LABEL_MAX_CHARS
        + _MANUAL_ISO_MAX_CHARS
        + _MANUAL_DISCUSSION_ROW_JSON_OVERHEAD_CHARS
    )
    + PERSON_STEP_URL_MAX_CHARS
    + PERSON_STEP_RESULT_MESSAGE_MAX_CHARS
    + _MANUAL_DISCUSSION_JSON_OVERHEAD_CHARS
) * MANUAL_RUN_STATE_BYTES_PER_CHAR
assert (
    2 * (MANUAL_RUN_STATE_SAFE_BYTES + MANUAL_DISCUSSION_SNAPSHOT_MAX_BYTES)
    <= MAX_MESSAGE_SIZE
), "MANUAL_DISCUSSION_SNAPSHOT_MAX_BYTES does not fit in MAX_MESSAGE_SIZE"


class ManualCaseActionKind(StrEnum):
    CLAIM = "claim"
    RELEASE = "release"
    # MANUAL_PAUSE_DISCUSS_V1: the tester takes a break / goes on testing.
    PAUSE = "pause"
    CONTINUE = "continue"


# The actions that act on a held claim and must carry its token.
_MANUAL_CLAIM_TOKEN_ACTIONS: Final[frozenset[ManualCaseActionKind]] = frozenset(
    {
        ManualCaseActionKind.RELEASE,
        ManualCaseActionKind.PAUSE,
        ManualCaseActionKind.CONTINUE,
    }
)


class ManualStepOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ManualShotKind(StrEnum):
    """What a screenshot of a hand-in shows: a passed or failed step, or the
    optional case screenshot taken with the verdict."""

    PASS = "pass"
    FAIL = "fail"
    CASE = "case"


class ManualCaseVerdictWire(StrEnum):
    """The tester's case verdict on the wire (server: ``ManualVerdict``)."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class ManualShotMime(StrEnum):
    PNG = "image/png"
    JPEG = "image/jpeg"


@dataclass(frozen=True)
class ManualRunInfoPayload:
    run_id: int
    name: str

    def to_wire(self) -> dict[str, object]:
        return {"id": self.run_id, "name": self.name}


@dataclass(frozen=True)
class ManualCaseListItemPayload:
    """One case of the manual run as the window's case list shows it."""

    pivot_id: int
    title: str
    # Blank when the case did not come from TestRail.
    testrail_id: str
    # A ``TestRunTestCaseStatus`` value (kept a string so a new server status
    # never breaks an older window).
    status: str
    # Who holds the claim ("Ana via Test Runner"); blank when unclaimed.
    claimed_label: str

    def to_wire(self) -> dict[str, object]:
        return {
            "pivot_id": self.pivot_id,
            "title": self.title,
            "testrail_id": self.testrail_id,
            "status": self.status,
            "claimed_label": self.claimed_label,
        }


@dataclass(frozen=True)
class ManualStepPayload:
    """One verbatim test case step (never reworded by the AI)."""

    num: int
    text: str
    # Blank when the case gives no expected result for the step.
    expected: str = ""

    def to_wire(self) -> dict[str, object]:
        return {
            "num": self.num,
            "text": self.text,
            "expected": self.expected,
        }


@dataclass(frozen=True)
class ManualDiscussionPayload:
    """The claimed case's discussion thread (MANUAL_PAUSE_DISCUSS_V1)."""

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
class ManualActiveCasePayload:
    """The case this runner holds the claim on."""

    pivot_id: int
    claim_token: str
    title: str
    preconditions: str
    steps: tuple[ManualStepPayload, ...]
    # ISO 8601; blank when unknown.
    claimed_at: str
    # MANUAL_PAUSE_DISCUSS_V1 (defaults from an older server): the case is
    # paused, by whom ("Ana via Test Runner") and since when (ISO 8601).
    paused: bool = False
    paused_by: str = ""
    paused_at: str = ""
    can_pause: bool = False
    can_continue: bool = False
    # Why Pause / Continue does not apply now (user-safe); "" when it does.
    pause_blocker: str = ""
    # None from an older server.
    discussion: ManualDiscussionPayload | None = None
    # Screenshots the server takes per marked step (1..MANUAL_STEP_MAX_SHOTS);
    # 1 from an older server.
    step_max_shots: int = 1

    def to_wire(self) -> dict[str, object]:
        return {
            "pivot_id": self.pivot_id,
            "claim_token": self.claim_token,
            "title": self.title,
            "preconditions": self.preconditions,
            "steps": [step.to_wire() for step in self.steps[:MANUAL_CASE_MAX_STEPS]],
            "claimed_at": self.claimed_at,
            "paused": self.paused,
            "paused_by": self.paused_by,
            "paused_at": self.paused_at,
            "can_pause": self.can_pause,
            "can_continue": self.can_continue,
            "pause_blocker": self.pause_blocker,
            "discussion": (
                self.discussion.to_wire() if self.discussion is not None else None
            ),
            "step_max_shots": self.step_max_shots,
        }


@dataclass(frozen=True)
class ManualPendingUploadPayload:
    """A hand-in of this project whose screenshots the server still waits for."""

    # Normalized UUID.
    submission_id: str
    missing: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "submission_id": self.submission_id,
            "missing": list(self.missing[:MANUAL_SUBMISSION_MAX_SHOTS]),
        }


@dataclass(frozen=True)
class ManualRunStateMessage:
    # None: the project has no manual run to show (the window leaves the
    # manual view); ``pending_uploads`` may still list hand-ins.
    run: ManualRunInfoPayload | None
    cases: tuple[ManualCaseListItemPayload, ...]
    active: ManualActiveCasePayload | None
    pending_uploads: tuple[ManualPendingUploadPayload, ...] = ()


@dataclass(frozen=True)
class ManualCaseActionPayload:
    action: ManualCaseActionKind
    pivot_id: int
    # The held claim (release, pause, continue); blank for claim.
    claim_token: str


@dataclass(frozen=True)
class ManualProgressStepPayload:
    """One step the tester has marked so far (advisory; no screenshots)."""

    num: int
    outcome: ManualStepOutcome
    # Stripped; may be empty (the hand-in, not this report, enforces notes).
    note: str

    def to_wire(self) -> dict[str, object]:
        return {"num": self.num, "outcome": self.outcome.value, "note": self.note}


@dataclass(frozen=True)
class ManualCaseProgressPayload:
    """The tester's marks so far on the claimed case (``manual_case_progress``)."""

    pivot_id: int
    claim_token: str
    # The step the window shows now; None when unknown.
    current_step: int | None
    # Distinct step numbers, ≤ MANUAL_CASE_MAX_STEPS.
    steps: tuple[ManualProgressStepPayload, ...]


@dataclass(frozen=True)
class ManualChatSendPayload:
    """A discussion turn about the claimed case (``manual_chat_send``)."""

    pivot_id: int
    claim_token: str
    # Stripped, 1..PERSON_CHAT_TEXT_MAX_CHARS.
    text: str


@dataclass(frozen=True)
class ManualCaseActionResultPayload:
    ok: bool
    # User-safe text to show in the window.
    message: str


@dataclass(frozen=True)
class ManualStepResultPayload:
    """One step the tester marked (unmarked steps are not sent: "Not run")."""

    num: int
    outcome: ManualStepOutcome
    # Stripped; required (non-empty) for a failed step.
    note: str
    # The step's screenshot keys (1..MANUAL_STEP_MAX_SHOTS), each listed in
    # the hand-in's ``shots`` with this step and a matching kind.
    shots: tuple[str, ...]
    # Time on the window's step timer, capped at MANUAL_STEP_MAX_SECONDS.
    seconds: int

    def to_wire(self) -> dict[str, object]:
        return {
            "num": self.num,
            "outcome": self.outcome.value,
            "note": self.note,
            "shots": list(self.shots),
            "seconds": self.seconds,
        }


@dataclass(frozen=True)
class ManualShotManifestPayload:
    """One screenshot a hand-in announces; uploaded afterwards."""

    key: str
    # None for the case screenshot.
    step_num: int | None
    kind: ManualShotKind
    # Lowercase hex of the raw image bytes.
    sha256: str
    size: int

    def to_wire(self) -> dict[str, object]:
        return {
            "key": self.key,
            "step_num": self.step_num,
            "kind": self.kind.value,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class ManualCaseSubmitPayload:
    """A case hand-in (``manual_case_submit``).

    The parser checks the structure and the note rules (D3/D4); the server
    also checks the claim, the case state and the step numbers against the
    stored plan.
    """

    # Normalized UUID minted by the runner; a replay returns the stored result.
    submission_id: str
    pivot_id: int
    claim_token: str
    # Ordered by step number, unique.
    steps: tuple[ManualStepResultPayload, ...]
    verdict: ManualCaseVerdictWire
    # Stripped; required for Blocked, and for Failed with no failed step.
    verdict_note: str
    shots: tuple[ManualShotManifestPayload, ...]


@dataclass(frozen=True)
class ManualCaseSubmitResultPayload:
    ok: bool
    # User-safe text to show in the window.
    message: str
    # The hand-in this answers (normalized UUID; "" when the message had none).
    submission_id: str
    # Screenshot keys the server still needs (all of them for a new hand-in).
    missing: tuple[str, ...] = ()
    # The server will never accept this hand-in (the claim was released, the
    # case re-run, ...): the runner deletes it and its screenshots.
    drop: bool = False


@dataclass(frozen=True)
class ManualScreenshotUploadPayload:
    submission_id: str
    key: str
    sha256: str
    mime: ManualShotMime
    # Decoded image bytes; length and sha256 already checked by the parser.
    data: bytes


@dataclass(frozen=True)
class ManualScreenshotAckPayload:
    submission_id: str
    key: str
    ok: bool
    # The server will never take any screenshot of this hand-in (superseded
    # or unknown): the runner deletes the whole hand-in.
    drop: bool
    # User-safe text; blank when ok.
    message: str
    # The server will never take this one screenshot (invalid image, too big,
    # not announced, checksum mismatch): the runner deletes only this key and
    # keeps uploading the hand-in's other screenshots.
    drop_shot: bool = False


def _extract_claim_token(data: Mapping[str, object]) -> str:
    value = data.get("claim_token")
    if not isinstance(value, str) or not _MANUAL_CLAIM_TOKEN_RE.fullmatch(value):
        raise ProtocolError("Missing or invalid 'claim_token'")
    return value


def _extract_optional_step_num(data: Mapping[str, object], field: str) -> int | None:
    value = data.get(field)
    if value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MANUAL_CASE_MAX_STEPS
    ):
        raise ProtocolError(f"Invalid '{field}': expected a step number or null")
    return value


def _extract_shot_key(value: object, field: str) -> str:
    if not isinstance(value, str) or not _MANUAL_SHOT_KEY_RE.fullmatch(value):
        raise ProtocolError(f"Missing or invalid '{field}': expected a shot key")
    return value


def _extract_submission_id(data: Mapping[str, object]) -> str:
    submission_id = _normalized_uuid(data.get("submission_id"))
    if submission_id is None:
        raise ProtocolError("Missing or invalid 'submission_id': expected a UUID")
    return submission_id


def _extract_sha256(data: Mapping[str, object]) -> str:
    value = data.get("sha256")
    if not isinstance(value, str) or not _SHA256_HEX_RE.fullmatch(value):
        raise ProtocolError("Missing or invalid 'sha256'")
    return value


def _extract_list(data: Mapping[str, object], field: str) -> list[object]:
    value = data.get(field, [])
    if not isinstance(value, list):
        raise ProtocolError(f"Invalid '{field}': expected a list")
    return value


def _extract_object(raw: object, name: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ProtocolError(f"Invalid {name}: expected an object")
    return raw


def _parse_manual_run(raw: dict[str, object] | None) -> ManualRunInfoPayload | None:
    if raw is None:
        return None
    return ManualRunInfoPayload(
        run_id=_extract_bounded_int(raw, "id", _PERSON_STEP_MAX_ID),
        name=_extract_text(raw, "name", PERSON_STEP_LABEL_MAX_CHARS, truncate=True),
    )


def _parse_manual_case_item(raw: object) -> ManualCaseListItemPayload:
    item = _extract_object(raw, "manual case")
    return ManualCaseListItemPayload(
        pivot_id=_extract_bounded_int(item, "pivot_id", _PERSON_STEP_MAX_ID),
        title=_extract_text(item, "title", MANUAL_CASE_TITLE_MAX_CHARS, truncate=True),
        testrail_id=_extract_text(
            item, "testrail_id", MANUAL_TESTRAIL_ID_MAX_CHARS, truncate=True
        ),
        status=_extract_text(
            item, "status", MANUAL_CASE_STATUS_MAX_CHARS, truncate=True
        ),
        claimed_label=_extract_text(
            item, "claimed_label", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
    )


def _parse_manual_step(raw: object) -> ManualStepPayload:
    item = _extract_object(raw, "manual step")
    return ManualStepPayload(
        num=_extract_bounded_int(item, "num", MANUAL_CASE_MAX_STEPS),
        text=_extract_text(item, "text", MANUAL_STEP_TEXT_MAX_CHARS, truncate=True),
        expected=_extract_text(
            item, "expected", MANUAL_STEP_TEXT_MAX_CHARS, truncate=True
        ),
    )


def _parse_manual_active(
    raw: dict[str, object] | None,
) -> ManualActiveCasePayload | None:
    if raw is None:
        return None
    return ManualActiveCasePayload(
        pivot_id=_extract_bounded_int(raw, "pivot_id", _PERSON_STEP_MAX_ID),
        claim_token=_extract_claim_token(raw),
        title=_extract_text(raw, "title", MANUAL_CASE_TITLE_MAX_CHARS, truncate=True),
        preconditions=_extract_text(
            raw, "preconditions", MANUAL_PRECONDITIONS_MAX_CHARS, truncate=True
        ),
        steps=tuple(
            _parse_manual_step(entry)
            for entry in _extract_list(raw, "steps")[:MANUAL_CASE_MAX_STEPS]
        ),
        claimed_at=_extract_text(
            raw, "claimed_at", _MANUAL_ISO_MAX_CHARS, truncate=True
        ),
        paused=_extract_bool(raw, "paused", default=False),
        paused_by=_extract_text(
            raw, "paused_by", PERSON_STEP_LABEL_MAX_CHARS, truncate=True
        ),
        paused_at=_extract_text(raw, "paused_at", _MANUAL_ISO_MAX_CHARS, truncate=True),
        can_pause=_extract_bool(raw, "can_pause", default=False),
        can_continue=_extract_bool(raw, "can_continue", default=False),
        pause_blocker=_extract_text(
            raw, "pause_blocker", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
        discussion=_parse_manual_discussion(
            _extract_optional_object(raw, "discussion")
        ),
        step_max_shots=_parse_step_max_shots(raw.get("step_max_shots")),
    )


def _parse_step_max_shots(raw: object) -> int:
    """Clamped to 1..MANUAL_STEP_MAX_SHOTS; 1 when absent or not an int."""
    if not isinstance(raw, int) or isinstance(raw, bool):
        return 1
    return max(1, min(raw, MANUAL_STEP_MAX_SHOTS))


def _parse_manual_discussion(
    raw: dict[str, object] | None,
) -> ManualDiscussionPayload | None:
    if raw is None:
        return None
    return ManualDiscussionPayload(
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
            for entry in _extract_list(raw, "messages")[
                -PERSON_DISCUSSION_MAX_MESSAGES:
            ]
        ),
    )


def _parse_manual_pending_upload(raw: object) -> ManualPendingUploadPayload:
    item = _extract_object(raw, "pending upload")
    return ManualPendingUploadPayload(
        submission_id=_extract_submission_id(item),
        missing=tuple(
            _extract_shot_key(key, "missing")
            for key in _extract_list(item, "missing")[:MANUAL_SUBMISSION_MAX_SHOTS]
        ),
    )


def parse_manual_run_state_payload(
    data: Mapping[str, object],
) -> ManualRunStateMessage:
    """Validate a ``manual_run_state`` (runner side).

    Lists beyond their caps are cut and over-long texts cut; a wrong type
    raises ProtocolError.
    """
    return ManualRunStateMessage(
        run=_parse_manual_run(_extract_optional_object(data, "run")),
        cases=tuple(
            _parse_manual_case_item(entry)
            for entry in _extract_list(data, "cases")[:MANUAL_RUN_STATE_MAX_CASES]
        ),
        active=_parse_manual_active(_extract_optional_object(data, "active")),
        pending_uploads=tuple(
            _parse_manual_pending_upload(entry)
            for entry in _extract_list(data, "pending_uploads")[
                :MANUAL_PENDING_UPLOADS_MAX
            ]
        ),
    )


def serialize_manual_run_state(state: ManualRunStateMessage) -> str:
    """A ``manual_run_state`` message (server side; fresh request_id)."""
    return serialize_message(
        MessageType.MANUAL_RUN_STATE,
        None,
        run=state.run.to_wire() if state.run is not None else None,
        cases=[case.to_wire() for case in state.cases[:MANUAL_RUN_STATE_MAX_CASES]],
        active=state.active.to_wire() if state.active is not None else None,
        pending_uploads=[
            upload.to_wire()
            for upload in state.pending_uploads[:MANUAL_PENDING_UPLOADS_MAX]
        ],
    )


def parse_manual_case_action_payload(
    data: Mapping[str, object],
) -> ManualCaseActionPayload:
    """Validate a ``manual_case_action`` (both sides).

    ``claim_token`` is required for ``release``, ``pause`` and ``continue``
    and dropped for ``claim``.
    """
    raw_action = data.get("action")
    if not isinstance(raw_action, str):
        raise ProtocolError("Missing or invalid 'action'")
    try:
        action = ManualCaseActionKind(raw_action)
    except ValueError as e:
        raise ProtocolError(f"Unknown manual case action: {raw_action[:40]!r}") from e
    claim_token = (
        _extract_claim_token(data) if action in _MANUAL_CLAIM_TOKEN_ACTIONS else ""
    )
    return ManualCaseActionPayload(
        action=action,
        pivot_id=_extract_bounded_int(data, "pivot_id", _PERSON_STEP_MAX_ID),
        claim_token=claim_token,
    )


def serialize_manual_case_action(
    action: ManualCaseActionPayload, request_id: str
) -> str:
    """A ``manual_case_action`` message (runner side; one request_id per action)."""
    return serialize_message(
        MessageType.MANUAL_CASE_ACTION,
        request_id,
        action=action.action.value,
        pivot_id=action.pivot_id,
        claim_token=action.claim_token,
    )


def _parse_manual_progress_step(raw: object) -> ManualProgressStepPayload:
    item = _extract_object(raw, "manual progress step")
    raw_outcome = item.get("outcome")
    if not isinstance(raw_outcome, str):
        raise ProtocolError("Missing or invalid 'outcome'")
    try:
        outcome = ManualStepOutcome(raw_outcome)
    except ValueError as e:
        raise ProtocolError(f"Unknown step outcome: {raw_outcome[:40]!r}") from e
    return ManualProgressStepPayload(
        num=_extract_bounded_int(item, "num", MANUAL_CASE_MAX_STEPS),
        outcome=outcome,
        note=_extract_text(
            item, "note", MANUAL_STEP_NOTE_MAX_CHARS, truncate=False
        ).strip(),
    )


def parse_manual_case_progress_payload(
    data: Mapping[str, object],
) -> ManualCaseProgressPayload:
    """Validate a ``manual_case_progress`` (both sides).

    More than MANUAL_CASE_MAX_STEPS steps, a repeated step number or an
    over-long note raises ProtocolError.
    """
    raw_steps = _extract_list(data, "steps")
    if len(raw_steps) > MANUAL_CASE_MAX_STEPS:
        raise ProtocolError(f"More than {MANUAL_CASE_MAX_STEPS} steps")
    steps = tuple(_parse_manual_progress_step(entry) for entry in raw_steps)
    if len({step.num for step in steps}) != len(steps):
        raise ProtocolError("A step is reported twice")
    return ManualCaseProgressPayload(
        pivot_id=_extract_bounded_int(data, "pivot_id", _PERSON_STEP_MAX_ID),
        claim_token=_extract_claim_token(data),
        current_step=_extract_optional_step_num(data, "current_step"),
        steps=steps,
    )


def serialize_manual_case_progress(
    progress: ManualCaseProgressPayload, request_id: str
) -> str:
    """A ``manual_case_progress`` message (runner side; no reply)."""
    return serialize_message(
        MessageType.MANUAL_CASE_PROGRESS,
        request_id,
        pivot_id=progress.pivot_id,
        claim_token=progress.claim_token,
        current_step=progress.current_step,
        steps=[step.to_wire() for step in progress.steps[:MANUAL_CASE_MAX_STEPS]],
    )


def parse_manual_chat_send_payload(
    data: Mapping[str, object],
) -> ManualChatSendPayload:
    """Validate a ``manual_chat_send`` (both sides); empty or over-long text raises."""
    text = _extract_text(
        data, "text", PERSON_CHAT_TEXT_MAX_CHARS, truncate=False
    ).strip()
    if not text:
        raise ProtocolError("Missing 'text'")
    return ManualChatSendPayload(
        pivot_id=_extract_bounded_int(data, "pivot_id", _PERSON_STEP_MAX_ID),
        claim_token=_extract_claim_token(data),
        text=text,
    )


def serialize_manual_chat_send(send: ManualChatSendPayload, request_id: str) -> str:
    """A ``manual_chat_send`` message (runner side; answered by
    ``manual_case_action_result`` with the same request_id)."""
    return serialize_message(
        MessageType.MANUAL_CHAT_SEND,
        request_id,
        pivot_id=send.pivot_id,
        claim_token=send.claim_token,
        text=send.text,
    )


def parse_manual_case_action_result_payload(
    data: Mapping[str, object],
) -> ManualCaseActionResultPayload:
    """Validate a ``manual_case_action_result`` (runner side)."""
    return ManualCaseActionResultPayload(
        ok=_extract_bool(dict(data), "ok"),
        message=_extract_text(
            data, "message", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
    )


def serialize_manual_case_action_result(
    result: ManualCaseActionResultPayload, request_id: str
) -> str:
    """A ``manual_case_action_result`` (server side; echoes the action's id)."""
    return serialize_message(
        MessageType.MANUAL_CASE_ACTION_RESULT,
        request_id,
        ok=result.ok,
        message=result.message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
    )


def _parse_manual_step_result(raw: object) -> ManualStepResultPayload:
    item = _extract_object(raw, "manual step result")
    raw_outcome = item.get("outcome")
    if not isinstance(raw_outcome, str):
        raise ProtocolError("Missing or invalid 'outcome'")
    try:
        outcome = ManualStepOutcome(raw_outcome)
    except ValueError as e:
        raise ProtocolError(f"Unknown step outcome: {raw_outcome[:40]!r}") from e
    num = _extract_bounded_int(item, "num", MANUAL_CASE_MAX_STEPS)
    note = _extract_text(
        item, "note", MANUAL_STEP_NOTE_MAX_CHARS, truncate=False
    ).strip()
    if outcome is ManualStepOutcome.FAIL and not note:
        raise ProtocolError(f"Step {num} failed without a note")
    raw_shots = _extract_list(item, "shots")
    if not 1 <= len(raw_shots) <= MANUAL_STEP_MAX_SHOTS:
        raise ProtocolError(
            f"Step {num} needs 1 to {MANUAL_STEP_MAX_SHOTS} screenshot keys"
        )
    raw_seconds = item.get("seconds", 0)
    if (
        not isinstance(raw_seconds, int)
        or isinstance(raw_seconds, bool)
        or raw_seconds < 0
    ):
        raise ProtocolError("Invalid 'seconds': expected a non-negative int")
    return ManualStepResultPayload(
        num=num,
        outcome=outcome,
        note=note,
        shots=tuple(_extract_shot_key(key, "shots") for key in raw_shots),
        seconds=min(raw_seconds, MANUAL_STEP_MAX_SECONDS),
    )


def _parse_manual_shot(raw: object) -> ManualShotManifestPayload:
    item = _extract_object(raw, "manual shot")
    raw_kind = item.get("kind")
    if not isinstance(raw_kind, str):
        raise ProtocolError("Missing or invalid 'kind'")
    try:
        kind = ManualShotKind(raw_kind)
    except ValueError as e:
        raise ProtocolError(f"Unknown shot kind: {raw_kind[:40]!r}") from e
    step_num = _extract_optional_step_num(item, "step_num")
    if (kind is ManualShotKind.CASE) != (step_num is None):
        raise ProtocolError("A case screenshot has no step; a step screenshot has one")
    size = item.get("size")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= MANUAL_SHOT_MAX_BYTES
    ):
        raise ProtocolError("Missing or invalid 'size'")
    return ManualShotManifestPayload(
        key=_extract_shot_key(item.get("key"), "key"),
        step_num=step_num,
        kind=kind,
        sha256=_extract_sha256(item),
        size=size,
    )


def _check_manual_manifest(
    steps: Sequence[ManualStepResultPayload],
    shots: Sequence[ManualShotManifestPayload],
) -> None:
    """Every step shot is announced once, with its step and outcome, and every
    announced shot belongs to a marked step or is the one case screenshot."""
    by_key: dict[str, ManualShotManifestPayload] = {}
    for shot in shots:
        if shot.key in by_key:
            raise ProtocolError("Duplicate screenshot key")
        by_key[shot.key] = shot
    if sum(1 for shot in shots if shot.kind is ManualShotKind.CASE) > (
        MANUAL_CASE_SHOT_MAX
    ):
        raise ProtocolError("Too many case screenshots")
    referenced: set[str] = set()
    for step in steps:
        expected_kind = (
            ManualShotKind.PASS
            if step.outcome is ManualStepOutcome.PASS
            else ManualShotKind.FAIL
        )
        for key in step.shots:
            announced = by_key.get(key)
            if (
                key in referenced
                or announced is None
                or announced.step_num != step.num
                or announced.kind is not expected_kind
            ):
                raise ProtocolError(f"Screenshot {key} does not match step {step.num}")
            referenced.add(key)
    for shot in shots:
        if shot.kind is not ManualShotKind.CASE and shot.key not in referenced:
            raise ProtocolError(f"Screenshot {shot.key} belongs to no marked step")


# Both sides run this parser: the runner before it spools a hand-in, the
# server on receipt, and a refusal drops the spooled hand-in. Tightening a rule
# here therefore requires a CLIENT_VERSION bump and keeping the old rule for
# older runners, or their already-spooled results would be deleted.
def parse_manual_case_submit_payload(
    data: Mapping[str, object],
) -> ManualCaseSubmitPayload:
    """Validate a ``manual_case_submit`` (both sides).

    Refused (ProtocolError): over-long notes, a failed step without a note,
    duplicate step numbers, a step without exactly its screenshots, a
    screenshot that matches no marked step, more than one case screenshot, a
    Blocked verdict without a note, and a Failed verdict without a note when
    no step failed. Notes are stripped.
    """
    raw_steps = _extract_list(data, "steps")
    if len(raw_steps) > MANUAL_CASE_MAX_STEPS:
        raise ProtocolError("Too many steps")
    steps = tuple(
        sorted(
            (_parse_manual_step_result(entry) for entry in raw_steps),
            key=lambda step: step.num,
        )
    )
    if len({step.num for step in steps}) != len(steps):
        raise ProtocolError("Duplicate step number")
    raw_shots = _extract_list(data, "shots")
    if len(raw_shots) > MANUAL_SUBMISSION_MAX_SHOTS:
        raise ProtocolError("Too many screenshots")
    shots = tuple(_parse_manual_shot(entry) for entry in raw_shots)
    _check_manual_manifest(steps, shots)
    raw_verdict = data.get("verdict")
    if not isinstance(raw_verdict, str):
        raise ProtocolError("Missing or invalid 'verdict'")
    try:
        verdict = ManualCaseVerdictWire(raw_verdict)
    except ValueError as e:
        raise ProtocolError(f"Unknown verdict: {raw_verdict[:40]!r}") from e
    verdict_note = _extract_text(
        data, "verdict_note", MANUAL_VERDICT_NOTE_MAX_CHARS, truncate=False
    ).strip()
    if not verdict_note:
        if verdict is ManualCaseVerdictWire.BLOCKED:
            raise ProtocolError("A Blocked verdict needs a note")
        if verdict is ManualCaseVerdictWire.FAILED and not any(
            step.outcome is ManualStepOutcome.FAIL for step in steps
        ):
            raise ProtocolError("A Failed verdict with no failed step needs a note")
    return ManualCaseSubmitPayload(
        submission_id=_extract_submission_id(data),
        pivot_id=_extract_bounded_int(data, "pivot_id", _PERSON_STEP_MAX_ID),
        claim_token=_extract_claim_token(data),
        steps=steps,
        verdict=verdict,
        verdict_note=verdict_note,
        shots=shots,
    )


def serialize_manual_case_submit(
    submit: ManualCaseSubmitPayload, request_id: str
) -> str:
    """A ``manual_case_submit`` message (runner side; one request_id per send)."""
    return serialize_message(
        MessageType.MANUAL_CASE_SUBMIT,
        request_id,
        submission_id=submit.submission_id,
        pivot_id=submit.pivot_id,
        claim_token=submit.claim_token,
        steps=[step.to_wire() for step in submit.steps],
        verdict=submit.verdict.value,
        verdict_note=submit.verdict_note,
        shots=[shot.to_wire() for shot in submit.shots],
    )


def parse_manual_case_submit_result_payload(
    data: Mapping[str, object],
) -> ManualCaseSubmitResultPayload:
    """Validate a ``manual_case_submit_result`` (runner side)."""
    return ManualCaseSubmitResultPayload(
        ok=_extract_bool(dict(data), "ok"),
        message=_extract_text(
            data, "message", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
        submission_id=_normalized_uuid(data.get("submission_id")) or "",
        missing=tuple(
            _extract_shot_key(key, "missing")
            for key in _extract_list(data, "missing")[:MANUAL_SUBMISSION_MAX_SHOTS]
        ),
        drop=_extract_bool(dict(data), "drop", default=False),
    )


def serialize_manual_case_submit_result(
    result: ManualCaseSubmitResultPayload, request_id: str
) -> str:
    """A ``manual_case_submit_result`` (server side; echoes the submit's id)."""
    return serialize_message(
        MessageType.MANUAL_CASE_SUBMIT_RESULT,
        request_id,
        ok=result.ok,
        message=result.message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
        submission_id=result.submission_id,
        missing=list(result.missing[:MANUAL_SUBMISSION_MAX_SHOTS]),
        drop=result.drop,
    )


def parse_manual_screenshot_upload_payload(
    data: Mapping[str, object],
) -> ManualScreenshotUploadPayload:
    """Validate a ``manual_screenshot_upload`` (server side).

    Decodes the base64 strictly and refuses (ProtocolError) an image over
    MANUAL_SHOT_MAX_BYTES or one whose sha256 does not match. The image
    format itself is checked by the server's service.
    """
    raw_mime = data.get("mime")
    if not isinstance(raw_mime, str):
        raise ProtocolError("Missing or invalid 'mime'")
    try:
        mime = ManualShotMime(raw_mime)
    except ValueError as e:
        raise ProtocolError(f"Unsupported image type: {raw_mime[:40]!r}") from e
    raw_data = data.get("data_b64")
    if not isinstance(raw_data, str):
        raise ProtocolError("Missing or invalid 'data_b64'")
    # Checked before decoding, so an oversized message is never decoded.
    if len(raw_data) > 4 * -(-MANUAL_SHOT_MAX_BYTES // 3):
        raise ProtocolError("Screenshot too large")
    try:
        image = base64.b64decode(raw_data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ProtocolError("Invalid 'data_b64': not base64") from e
    if not 1 <= len(image) <= MANUAL_SHOT_MAX_BYTES:
        raise ProtocolError("Screenshot empty or too large")
    sha256 = _extract_sha256(data)
    if hashlib.sha256(image).hexdigest() != sha256:
        raise ProtocolError("Screenshot does not match its sha256")
    return ManualScreenshotUploadPayload(
        submission_id=_extract_submission_id(data),
        key=_extract_shot_key(data.get("key"), "key"),
        sha256=sha256,
        mime=mime,
        data=image,
    )


def serialize_manual_screenshot_upload(
    upload: ManualScreenshotUploadPayload, request_id: str
) -> str:
    """A ``manual_screenshot_upload`` message (runner side; one request_id per
    upload). The caller keeps ``upload.data`` within MANUAL_SHOT_MAX_BYTES."""
    return serialize_message(
        MessageType.MANUAL_SCREENSHOT_UPLOAD,
        request_id,
        submission_id=upload.submission_id,
        key=upload.key,
        sha256=upload.sha256,
        mime=upload.mime.value,
        data_b64=base64.b64encode(upload.data).decode("ascii"),
    )


def peek_manual_upload_ids(data: Mapping[str, object]) -> tuple[str, str] | None:
    """The ``(submission_id, key)`` of an upload message, or None.

    Lets the server answer a refused upload with an ack the runner can match
    even when the rest of the message is invalid.
    """
    submission_id = _normalized_uuid(data.get("submission_id"))
    key = data.get("key")
    if (
        submission_id is None
        or not isinstance(key, str)
        or not _MANUAL_SHOT_KEY_RE.fullmatch(key)
    ):
        return None
    return submission_id, key


def parse_manual_screenshot_ack_payload(
    data: Mapping[str, object],
) -> ManualScreenshotAckPayload:
    """Validate a ``manual_screenshot_ack`` (runner side)."""
    return ManualScreenshotAckPayload(
        submission_id=_extract_submission_id(data),
        key=_extract_shot_key(data.get("key"), "key"),
        ok=_extract_bool(dict(data), "ok"),
        drop=_extract_bool(dict(data), "drop", default=False),
        message=_extract_text(
            data, "message", PERSON_STEP_RESULT_MESSAGE_MAX_CHARS, truncate=True
        ),
        drop_shot=_extract_bool(dict(data), "drop_shot", default=False),
    )


def serialize_manual_screenshot_ack(
    ack: ManualScreenshotAckPayload, request_id: str
) -> str:
    """A ``manual_screenshot_ack`` (server side; echoes the upload's id)."""
    return serialize_message(
        MessageType.MANUAL_SCREENSHOT_ACK,
        request_id,
        submission_id=ack.submission_id,
        key=ack.key,
        ok=ack.ok,
        drop=ack.drop,
        message=ack.message[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
        drop_shot=ack.drop_shot,
    )


# --- Phone and tablet target (MOBILE_DEVICE_V1) -----------------------------
#
# The runner keeps running on a host computer and drives one phone, tablet,
# emulator or simulator (``CONTROLLER_TARGET``). ``system_info.os`` stays the
# host OS; the keys below describe the device. Coordinates are screenshot
# pixels, as for every desktop pointer action.


class RunnerTarget(StrEnum):
    DESKTOP = "desktop"
    ANDROID = "android"
    IOS = "ios"


MOBILE_TARGETS: Final[frozenset[RunnerTarget]] = frozenset(
    {RunnerTarget.ANDROID, RunnerTarget.IOS}
)


class DeviceKind(StrEnum):
    PHYSICAL = "physical"
    EMULATOR = "emulator"
    SIMULATOR = "simulator"


class DeviceState(StrEnum):
    READY = "ready"
    # The Appium session is being created (a first WebDriverAgent build takes
    # minutes).
    STARTING = "starting"
    DISCONNECTED = "disconnected"
    # USB debugging not authorised, or the iPhone does not trust the host.
    UNAUTHORIZED = "unauthorized"
    # Appium or the session failed; ``detail`` says why.
    UNAVAILABLE = "unavailable"


class MobileButton(StrEnum):
    BACK = "back"
    HOME = "home"
    APP_SWITCH = "app_switch"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    POWER = "power"


class DeviceOrientation(StrEnum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


# The runner sends ``device_status`` on every change and at least this often.
DEVICE_STATUS_INTERVAL_SECONDS: Final[int] = 30
DEVICE_STATUS_DETAIL_MAX_CHARS: Final[int] = 500
# Longest value of one device key in ``system_info``; longer values are dropped.
DEVICE_INFO_VALUE_MAX_CHARS: Final[int] = 100

# ``system_info`` keys of a phone target. ``screen_width`` / ``screen_height``
# are then the device's screenshot pixels in portrait.
SYSTEM_INFO_TARGET: Final[str] = "target"
SYSTEM_INFO_DEVICE_PLATFORM: Final[str] = "device_platform"
SYSTEM_INFO_DEVICE_KIND: Final[str] = "device_kind"
SYSTEM_INFO_DEVICE_MODEL: Final[str] = "device_model"
SYSTEM_INFO_DEVICE_NAME: Final[str] = "device_name"
SYSTEM_INFO_DEVICE_OS_VERSION: Final[str] = "device_os_version"
SYSTEM_INFO_DEVICE_ID: Final[str] = "device_id"
SYSTEM_INFO_DEVICE_SCALE: Final[str] = "device_scale"

# Swipe length as a share of the screen's side along the swipe.
SWIPE_MIN_DISTANCE: Final[float] = 0.1
SWIPE_MAX_DISTANCE: Final[float] = 0.9
DEFAULT_SWIPE_DISTANCE: Final[float] = 0.5
SWIPE_MIN_DURATION: Final[float] = 0.05
SWIPE_MAX_DURATION: Final[float] = 5.0
DEFAULT_SWIPE_DURATION: Final[float] = 0.4
LONG_PRESS_MIN_SECONDS: Final[float] = 0.5
LONG_PRESS_MAX_SECONDS: Final[float] = 10.0
DEFAULT_LONG_PRESS_SECONDS: Final[float] = 1.0
# Seconds a coordinate drag (drag_at) lasts; the server bounds it, the runner
# accepts any non-negative duration in the drag message.
DRAG_MIN_SECONDS: Final[float] = 0.1
DRAG_MAX_SECONDS: Final[float] = 10.0
DEFAULT_DRAG_SECONDS: Final[float] = 0.5
OPEN_URL_MAX_CHARS: Final[int] = PERSON_STEP_URL_MAX_CHARS

_URL_SCHEME_RE: Final[re.Pattern[str]] = re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,31}):")
# Schemes that run code or read local files in the device's browser.
_REFUSED_URL_SCHEMES: Final[frozenset[str]] = frozenset(
    # ``intent:`` / ``android-app:`` start arbitrary Android activities;
    # launch_app covers apps.
    {
        "javascript",
        "file",
        "data",
        "vbscript",
        "content",
        "blob",
        "about",
        "intent",
        "android-app",
    }
)


@dataclass(frozen=True)
class DeviceInfoPayload:
    """The device a phone runner drives, as reported in ``system_info``."""

    target: RunnerTarget
    platform: str
    kind: str
    model: str
    name: str
    os_version: str
    device_id: str
    scale: str


@dataclass(frozen=True)
class SwipePayload:
    # The finger's direction: ``up`` moves the finger up, which scrolls the
    # content down (reveals what is below).
    direction: ScrollDirection
    # Start point in screenshot pixels; both None: the centre of the screen.
    x: int | None
    y: int | None
    # SWIPE_MIN_DISTANCE..SWIPE_MAX_DISTANCE of the screen's side.
    distance: float
    duration: float


@dataclass(frozen=True)
class LongPressPayload:
    x: int
    y: int
    duration: float


@dataclass(frozen=True)
class PressButtonPayload:
    button: MobileButton


@dataclass(frozen=True)
class RotateDevicePayload:
    orientation: DeviceOrientation


@dataclass(frozen=True)
class OpenUrlPayload:
    url: str


@dataclass(frozen=True)
class InstallAppPayload:
    # An absolute path on the runner host: ``.apk`` (Android), a simulator
    # ``.app`` or a device-signed ``.ipa`` (iOS). Network paths are refused.
    path: str


@dataclass(frozen=True)
class DeviceStatusPayload:
    state: DeviceState
    detail: str


def parse_runner_target(system_info: Mapping[str, object]) -> RunnerTarget:
    """The target of a runner from its handshake ``system_info``.

    Every runner older than 0.19.0 and every desktop runner sends no
    ``target`` key and reads ``desktop``; so does an unknown value.
    """
    raw = system_info.get(SYSTEM_INFO_TARGET)
    if not isinstance(raw, str):
        return RunnerTarget.DESKTOP
    try:
        return RunnerTarget(raw)
    except ValueError:
        return RunnerTarget.DESKTOP


def is_mobile_target(system_info: Mapping[str, object]) -> bool:
    return parse_runner_target(system_info) in MOBILE_TARGETS


def _device_value(system_info: Mapping[str, object], key: str) -> str:
    value = system_info.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = str(value).strip()
    if len(text) > DEVICE_INFO_VALUE_MAX_CHARS or not text.isprintable():
        return ""
    return text


def parse_device_info(system_info: Mapping[str, object]) -> DeviceInfoPayload | None:
    """The device of a phone runner, or None for a desktop runner.

    The one definition every server reader uses. Values longer than
    DEVICE_INFO_VALUE_MAX_CHARS or with control characters read empty.
    """
    target = parse_runner_target(system_info)
    if target not in MOBILE_TARGETS:
        return None
    return DeviceInfoPayload(
        target=target,
        platform=_device_value(system_info, SYSTEM_INFO_DEVICE_PLATFORM),
        kind=_device_value(system_info, SYSTEM_INFO_DEVICE_KIND),
        model=_device_value(system_info, SYSTEM_INFO_DEVICE_MODEL),
        name=_device_value(system_info, SYSTEM_INFO_DEVICE_NAME),
        os_version=_device_value(system_info, SYSTEM_INFO_DEVICE_OS_VERSION),
        device_id=_device_value(system_info, SYSTEM_INFO_DEVICE_ID),
        scale=_device_value(system_info, SYSTEM_INFO_DEVICE_SCALE),
    )


def device_info_to_system_info(info: DeviceInfoPayload) -> dict[str, str]:
    """The ``system_info`` keys of a phone runner (runner side)."""
    values = {
        SYSTEM_INFO_TARGET: info.target.value,
        SYSTEM_INFO_DEVICE_PLATFORM: info.platform,
        SYSTEM_INFO_DEVICE_KIND: info.kind,
        SYSTEM_INFO_DEVICE_MODEL: info.model,
        SYSTEM_INFO_DEVICE_NAME: info.name,
        SYSTEM_INFO_DEVICE_OS_VERSION: info.os_version,
        SYSTEM_INFO_DEVICE_ID: info.device_id,
        SYSTEM_INFO_DEVICE_SCALE: info.scale,
    }
    return {
        key: value[:DEVICE_INFO_VALUE_MAX_CHARS]
        for key, value in values.items()
        if key == SYSTEM_INFO_TARGET or value
    }


def _extract_bounded_number(
    data: dict[str, object],
    field: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = data.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"Missing or invalid '{field}'")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ProtocolError(f"Invalid '{field}': expected {minimum} to {maximum}")
    return number


def parse_swipe_payload(data: dict[str, object]) -> SwipePayload:
    raw_direction = _extract_str(data, "direction")
    try:
        direction = ScrollDirection(raw_direction)
    except ValueError as e:
        raise ProtocolError(f"Unknown swipe direction: {raw_direction}") from e
    x = _extract_optional_int(data, "x")
    y = _extract_optional_int(data, "y")
    if (x is None) != (y is None):
        raise ProtocolError("'x' and 'y' must both be given or both be omitted")
    return SwipePayload(
        direction=direction,
        x=x,
        y=y,
        distance=_extract_bounded_number(
            data,
            "distance",
            DEFAULT_SWIPE_DISTANCE,
            SWIPE_MIN_DISTANCE,
            SWIPE_MAX_DISTANCE,
        ),
        duration=_extract_bounded_number(
            data,
            "duration",
            DEFAULT_SWIPE_DURATION,
            SWIPE_MIN_DURATION,
            SWIPE_MAX_DURATION,
        ),
    )


def parse_long_press_payload(data: dict[str, object]) -> LongPressPayload:
    return LongPressPayload(
        x=_extract_int(data, "x"),
        y=_extract_int(data, "y"),
        duration=_extract_bounded_number(
            data,
            "duration",
            DEFAULT_LONG_PRESS_SECONDS,
            LONG_PRESS_MIN_SECONDS,
            LONG_PRESS_MAX_SECONDS,
        ),
    )


def parse_press_button_payload(data: dict[str, object]) -> PressButtonPayload:
    raw = _extract_str(data, "button")
    try:
        return PressButtonPayload(button=MobileButton(raw))
    except ValueError as e:
        raise ProtocolError(f"Unknown button: {raw}") from e


def parse_rotate_device_payload(data: dict[str, object]) -> RotateDevicePayload:
    raw = _extract_str(data, "orientation")
    try:
        return RotateDevicePayload(orientation=DeviceOrientation(raw))
    except ValueError as e:
        raise ProtocolError(f"Unknown orientation: {raw}") from e


def validate_open_url(url: str) -> str:
    """``url`` stripped, or ProtocolError when the device must not open it.

    http(s) and custom app schemes (deep links) pass; schemes that run code
    or read local files (``javascript:``, ``file:``, ``data:``, ...) do not.
    """
    text = url.strip()
    if not text or len(text) > OPEN_URL_MAX_CHARS:
        raise ProtocolError("Invalid 'url': empty or too long")
    if any(not ch.isprintable() or ch.isspace() for ch in text):
        raise ProtocolError("Invalid 'url': contains spaces or control characters")
    match = _URL_SCHEME_RE.match(text)
    if match is None:
        raise ProtocolError("Invalid 'url': it needs a scheme such as https://")
    if match.group(1).lower() in _REFUSED_URL_SCHEMES:
        raise ProtocolError(f"Invalid 'url': the {match.group(1)}: scheme is refused")
    return text


def parse_open_url_payload(data: dict[str, object]) -> OpenUrlPayload:
    return OpenUrlPayload(url=validate_open_url(_extract_str(data, "url")))


def parse_install_app_payload(data: dict[str, object]) -> InstallAppPayload:
    path = _extract_path(data, "path")
    if is_network_path(path):
        raise ProtocolError("Invalid 'path': network paths are refused")
    return InstallAppPayload(path=path)


def parse_device_status_payload(data: Mapping[str, object]) -> DeviceStatusPayload:
    """Validate a ``device_status`` (server side); ``detail`` is truncated."""
    raw = data.get("state")
    if not isinstance(raw, str):
        raise ProtocolError("Missing or invalid 'state'")
    try:
        state = DeviceState(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown device state: {raw}") from e
    detail = data.get("detail", "")
    if not isinstance(detail, str):
        raise ProtocolError("Invalid 'detail'")
    cleaned = "".join(ch for ch in detail if ch.isprintable() or ch in "\n\t")
    return DeviceStatusPayload(
        state=state, detail=cleaned.strip()[:DEVICE_STATUS_DETAIL_MAX_CHARS]
    )


def serialize_device_status(status: DeviceStatusPayload) -> str:
    """A ``device_status`` (runner side)."""
    return serialize_message(
        MessageType.DEVICE_STATUS,
        state=status.state.value,
        detail=status.detail[:DEVICE_STATUS_DETAIL_MAX_CHARS],
    )


# --- Window capture (WINDOW_CAPTURE_V1) -------------------------------------
#
# The server asks the runner for the allowlisted windows (``list_windows``)
# or for one capture (``capture_window``). Requests carry only the target
# description, the project's allowlist and the limits: never a pivot, claim,
# run, user or client detail. The runner answers ``window_list_result`` /
# ``window_capture_result`` with ``success: true`` and the result object (the
# ``serialize_window_*_result`` JSON form) under ``result``; a refusal is a
# ``status`` other than ``ok`` inside that object, never an ``error`` message.
# The server re-validates every result with the ``parse_window_*_result``
# functions and checks the reported process against the allowlist again.

# Most process names in one project's allowlist.
CAPTURE_ALLOWED_PROCESSES_MAX: Final[int] = 20
# Longest normalized process name (``normalize_process_name``).
CAPTURE_PROCESS_NAME_MAX_CHARS: Final[int] = 64
# Longest ``title_contains`` a request may carry (refused when longer).
CAPTURE_TITLE_MATCH_MAX_CHARS: Final[int] = 200
# Longest window title a result carries (cut on both sides).
CAPTURE_TITLE_MAX_CHARS: Final[int] = 256
# Longest platform window id (HWND, CGWindowID, X11 id, sway con id, Hyprland
# address, GNOME window id, or "device" on a phone), as a string.
CAPTURE_WINDOW_ID_MAX_CHARS: Final[int] = 64
# Longest virtual desktop name a result carries (cut on both sides).
CAPTURE_DESKTOP_NAME_MAX_CHARS: Final[int] = 64
# Longest ``detail`` a result carries (cut on both sides). Kept small so a
# capture result fits WINDOW_CAPTURE_RESULT_MAX_BYTES even when every
# character is JSON-escaped.
CAPTURE_DETAIL_MAX_CHARS: Final[int] = 500
# Most windows one ``window_list_result`` carries (topmost first).
WINDOW_LIST_MAX: Final[int] = 50
# Longest extra wait after the window settled, in milliseconds.
CAPTURE_DELAY_MAX_MS: Final[int] = 5000
# Largest region edge, and largest window or screen edge a result reports.
CAPTURE_REGION_MAX_PIXELS_PER_AXIS: Final[int] = 16384
# Largest window edge a ``window_list_result`` reports (clamped by the
# serializer; a window may extend past its monitors).
_CAPTURE_WINDOW_SIZE_MAX: Final[int] = 65535
# Highest 1-based virtual desktop index a result may report.
_CAPTURE_DESKTOP_INDEX_MAX: Final[int] = 1000
# Highest ``other_matches`` / ``others_hidden`` count a result may report.
_CAPTURE_COUNT_MAX: Final[int] = 100_000
# Room for the rest of a ``window_capture_result`` besides the base64 text
# (title, detail, desktop name and the fixed fields, all JSON-escaped).
_WINDOW_CAPTURE_JSON_OVERHEAD_BYTES: Final[int] = 8 * 1024
# Largest raw ``window_capture_result`` message the server reads: the padded
# base64 size of MANUAL_SHOT_MAX_BYTES plus the overhead.
WINDOW_CAPTURE_RESULT_MAX_BYTES: Final[int] = (
    4 * -(-MANUAL_SHOT_MAX_BYTES // 3) + _WINDOW_CAPTURE_JSON_OVERHEAD_BYTES
)
assert (
    WINDOW_CAPTURE_RESULT_MAX_BYTES <= MAX_MESSAGE_SIZE
), "WINDOW_CAPTURE_RESULT_MAX_BYTES does not fit in MAX_MESSAGE_SIZE"
# Largest raw ``window_list_result`` message the server reads.
WINDOW_LIST_RESULT_MAX_BYTES: Final[int] = 256 * 1024
# The ``window_id`` of the single entry a phone runner lists.
PHONE_WINDOW_ID: Final[str] = "device"

_PROCESS_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9 ._+-]+")
_PROCESS_NAME_SUFFIXES: Final[tuple[str, ...]] = (".exe", ".app")
# Raw names longer than this are refused before any work.
_PROCESS_NAME_RAW_MAX_CHARS: Final[int] = 4096


def normalize_process_name(raw: str) -> str | None:
    """The allowlist form of a process name, or None when it is not valid.

    Takes the stripped basename (``/`` and ``\\`` both separate), lower-cased,
    without one trailing ``.exe`` or ``.app``. Refuses an empty name, one longer
    than CAPTURE_PROCESS_NAME_MAX_CHARS, one made only of dots and spaces, and
    one with characters outside ``[a-z0-9 ._+-]``. ``C:\\...\\firefox.exe``,
    ``firefox.exe``, ``Firefox`` and ``firefox`` all give ``firefox``;
    ``Microsoft Edge`` (a macOS owner name) gives ``microsoft edge``.

    The one definition of a process name: the server's Settings form, the
    runner's allowlist matching and both result parsers use it.
    """
    if len(raw) > _PROCESS_NAME_RAW_MAX_CHARS:
        return None
    name = raw.strip().replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    for suffix in _PROCESS_NAME_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)].rstrip()
            break
    if (
        not name
        or len(name) > CAPTURE_PROCESS_NAME_MAX_CHARS
        or not _PROCESS_NAME_RE.fullmatch(name)
        or not name.strip(". ")
    ):
        return None
    return name


class CaptureTarget(StrEnum):
    # The matched window only (``region`` must be absent).
    WINDOW = "window"
    # The whole monitor holding the matched (or topmost allowlisted) window,
    # refused with ``screen_not_clean`` when a foreign window is visible there.
    SCREEN = "screen"
    # A rectangle of the matched window's shot, in its image pixels (a phone:
    # of the screen).
    REGION = "region"


class CaptureStatus(StrEnum):
    """Outcome of ``list_windows`` / ``capture_window``; only ``ok`` carries
    an image. The detail conventions are on WindowCaptureResultPayload."""

    OK = "ok"
    # The match is not allowlisted, or denied (runner, terminal, OS shell).
    NOT_ALLOWED = "not_allowed"
    WINDOW_NOT_FOUND = "window_not_found"
    WINDOW_MINIMIZED = "window_minimized"
    # Only when the request's ``switch_desktop`` is false.
    WINDOW_ON_OTHER_DESKTOP = "window_on_other_desktop"
    # The switch could not be done or verified (elevated window in front,
    # desktop order unreadable, Accessibility missing, auto-switch off, IPC or
    # helper refused, no window to return to).
    DESKTOP_SWITCH_FAILED = "desktop_switch_failed"
    # After the switch the window kept changing past the freshness budget.
    WINDOW_NOT_SETTLED = "window_not_settled"
    # Covered, and the platform has no faithful window-only capture.
    WINDOW_OBSCURED = "window_obscured"
    REGION_OUT_OF_BOUNDS = "region_out_of_bounds"
    SCREEN_NOT_CLEAN = "screen_not_clean"
    # Session 0, locked screen, secure desktop, disconnected session, no
    # display or session bus.
    NOT_INTERACTIVE = "not_interactive"
    # macOS Screen Recording not granted.
    PERMISSION_MISSING = "permission_missing"
    # All black or uniform; never sent.
    CAPTURE_BLANK = "capture_blank"
    # The person window could not be confirmed hidden.
    HIDE_FAILED = "hide_failed"
    # Still over ``max_bytes`` after every downscale step.
    TOO_LARGE = "too_large"
    # KDE Wayland or an unknown Wayland compositor.
    COMPOSITOR_UNSUPPORTED = "compositor_unsupported"
    # The GNOME helper extension is not installed, enabled or current, or a
    # wlroots tool (swaymsg, hyprctl, grim) is missing.
    HELPER_MISSING = "helper_missing"
    # A phone asked for ``window``.
    UNSUPPORTED = "unsupported"
    # The request itself was invalid (the runner could not parse it).
    INVALID = "invalid"
    # The platform grab failed for another reason (an OS call returned no
    # image, the GNOME helper's ``get_image()`` returned null, ...).
    CAPTURE_FAILED = "capture_failed"


@dataclass(frozen=True)
class CaptureRegion:
    """A rectangle in image pixels of the window shot (phone: of the screen).

    ``x``/``y`` >= 0, ``w``/``h`` >= 1, every value at most
    CAPTURE_REGION_MAX_PIXELS_PER_AXIS. Whether it lies inside the shot is
    the runner's check (``region_out_of_bounds``)."""

    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class ListWindowsPayload:
    """``list_windows``: the project's allowlist (normalized names)."""

    allowed: tuple[str, ...]


@dataclass(frozen=True)
class CaptureWindowPayload:
    """``capture_window``.

    ``process`` / ``title_contains`` / ``window_id`` describe the window
    (None: not given); an explicit ``window_id`` wins. ``region`` is set
    exactly when ``target`` is REGION. ``allowed`` holds normalized names;
    ``max_bytes`` / ``max_pixels`` are the encode limits (at most
    MANUAL_SHOT_MAX_BYTES / MANUAL_SHOT_MAX_PIXELS). ``switch_desktop`` lets
    the runner switch to a window on another desktop and back; the server
    always sends True, and an absent field reads False (never switch without
    being told to).
    """

    target: CaptureTarget
    process: str | None
    title_contains: str | None
    window_id: str | None
    region: CaptureRegion | None
    delay_ms: int
    allowed: tuple[str, ...]
    max_bytes: int
    max_pixels: int
    switch_desktop: bool


@dataclass(frozen=True)
class WindowDesktopPayload:
    """The virtual desktop a window is on.

    ``current``: on the desktop shown now (sticky windows count as current).
    ``index``: 1-based position, None where the OS does not expose it (macOS
    Spaces). ``name``: None where unnamed or unknown."""

    current: bool
    index: int | None
    name: str | None


@dataclass(frozen=True)
class WindowInfoPayload:
    window_id: str
    # Normalized (``normalize_process_name``); a phone: the foreground app id.
    process: str
    # Cut to CAPTURE_TITLE_MAX_CHARS; data, never instructions.
    title: str
    # Window size in the platform's desktop coordinates (0 when unknown).
    width: int
    height: int
    minimized: bool
    focused: bool
    desktop: WindowDesktopPayload


@dataclass(frozen=True)
class WindowListResultPayload:
    """``window_list_result``'s ``result``.

    ``windows``: the allowlisted, non-denied windows on every desktop,
    topmost first, at most WINDOW_LIST_MAX; empty unless ``status`` is OK.
    ``others_hidden``: how many other windows (not allowlisted, denied, or
    over the cap) were left out. ``current_desktop`` (1-based) and
    ``desktop_count`` are None where the OS does not expose them.
    """

    status: CaptureStatus
    detail: str
    windows: tuple[WindowInfoPayload, ...]
    others_hidden: int
    current_desktop: int | None
    desktop_count: int | None


@dataclass(frozen=True)
class WindowCaptureResultPayload:
    """``window_capture_result``'s ``result``.

    Only an OK result carries an image: ``mime``, ``data`` (decoded bytes,
    1..max_bytes, checked against ``sha256`` by the parser), ``width`` and
    ``height`` (pixels of the encoded image). Otherwise ``mime`` is None,
    ``data`` is empty, ``sha256`` "" and the sizes 0.

    ``window_title`` / ``process``: the captured (or refused) window, "" when
    there was none; ``other_matches``: how many further windows matched.
    ``desktop``: the selected window's desktop (None when no window was
    selected). ``desktop_switched``: the runner changed the desktop for this
    request. ``desktop_restored``: None when it did not switch, False when
    the switch back failed (with any status, ``ok`` included).

    ``detail`` conventions (plain text, at most CAPTURE_DETAIL_MAX_CHARS):
    ``screen_not_clean``: the foreign processes shown, normalized, joined by
    ", "; ``region_out_of_bounds``: the shot size as ``"<w>x<h>"``;
    ``desktop_switch_failed``, ``not_interactive``, ``permission_missing``,
    ``compositor_unsupported``, ``helper_missing``: the runner's fixed,
    user-safe explanation; anything else: a short hint or "". Never a window
    title.
    """

    status: CaptureStatus
    detail: str
    mime: ManualShotMime | None
    data: bytes
    sha256: str
    width: int
    height: int
    window_title: str
    process: str
    other_matches: int
    desktop: WindowDesktopPayload | None
    desktop_switched: bool
    desktop_restored: bool | None


def _extract_optional_request_text(
    data: Mapping[str, object], field: str, max_chars: int
) -> str | None:
    """A request's optional string: None when absent, null or blank;
    refused when longer than ``max_chars`` or not a string."""
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"Invalid '{field}': expected a string")
    if len(value) > max_chars:
        raise ProtocolError(f"Invalid '{field}': longer than {max_chars} characters")
    return value if value.strip() else None


def _extract_ranged_int(
    data: Mapping[str, object],
    field: str,
    minimum: int,
    maximum: int,
    default: int | None = None,
) -> int:
    value = data.get(field, default)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(
            f"Missing or invalid '{field}': expected an int in {minimum}..{maximum}"
        )
    return value


def _extract_optional_ranged_int(
    data: Mapping[str, object], field: str, minimum: int, maximum: int
) -> int | None:
    if data.get(field) is None:
        return None
    return _extract_ranged_int(data, field, minimum, maximum)


def _extract_optional_flag(data: Mapping[str, object], field: str) -> bool | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ProtocolError(f"Invalid '{field}': expected a bool or null")
    return value


def _extract_process_name(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"Missing or invalid '{field}': expected a string")
    name = normalize_process_name(value)
    if name is None:
        raise ProtocolError(f"Invalid '{field}': not a valid process name")
    return name


def _extract_allowed(data: Mapping[str, object]) -> tuple[str, ...]:
    """0..CAPTURE_ALLOWED_PROCESSES_MAX normalized names, duplicates removed."""
    raw = data.get("allowed", [])
    if not isinstance(raw, list) or len(raw) > CAPTURE_ALLOWED_PROCESSES_MAX:
        raise ProtocolError(
            "Invalid 'allowed': expected a list of at most "
            f"{CAPTURE_ALLOWED_PROCESSES_MAX} process names"
        )
    names = (_extract_process_name(item, "allowed") for item in raw)
    return tuple(dict.fromkeys(names))


def _clean_result_text(value: str, max_chars: int) -> str:
    """Printable characters only, stripped and cut to ``max_chars``."""
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned.strip()[:max_chars]


def _extract_result_text(data: Mapping[str, object], field: str, max_chars: int) -> str:
    value = data.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProtocolError(f"Invalid '{field}': expected a string")
    return _clean_result_text(value, max_chars)


def _extract_status(data: Mapping[str, object]) -> CaptureStatus:
    raw = data.get("status")
    if not isinstance(raw, str):
        raise ProtocolError("Missing or invalid 'status'")
    try:
        return CaptureStatus(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown capture status: {raw[:40]!r}") from e


def _extract_window_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > CAPTURE_WINDOW_ID_MAX_CHARS
        or not value.isprintable()
    ):
        raise ProtocolError(f"Missing or invalid '{field}': expected a window id")
    return value


def _parse_region(raw: object) -> CaptureRegion:
    region = _extract_object(raw, "region")
    limit = CAPTURE_REGION_MAX_PIXELS_PER_AXIS
    return CaptureRegion(
        x=_extract_ranged_int(region, "x", 0, limit),
        y=_extract_ranged_int(region, "y", 0, limit),
        w=_extract_ranged_int(region, "w", 1, limit),
        h=_extract_ranged_int(region, "h", 1, limit),
    )


def parse_list_windows_payload(data: Mapping[str, object]) -> ListWindowsPayload:
    """Validate a ``list_windows`` request (runner side)."""
    return ListWindowsPayload(allowed=_extract_allowed(data))


def serialize_list_windows_payload(payload: ListWindowsPayload) -> dict[str, object]:
    """The JSON fields of a ``list_windows`` request (server side; passed as
    the action's keyword payload)."""
    return {"allowed": list(payload.allowed)}


def parse_capture_window_payload(data: Mapping[str, object]) -> CaptureWindowPayload:
    """Validate a ``capture_window`` request (runner side).

    Refuses (ProtocolError) an unknown target, a region without target
    ``region`` or the reverse, limits out of range and invalid names; the
    runner answers that with status ``invalid``.
    """
    raw_target = data.get("target")
    if not isinstance(raw_target, str):
        raise ProtocolError("Missing or invalid 'target'")
    try:
        target = CaptureTarget(raw_target)
    except ValueError as e:
        raise ProtocolError(f"Unknown capture target: {raw_target[:40]!r}") from e
    raw_region = data.get("region")
    region = None if raw_region is None else _parse_region(raw_region)
    if (region is None) != (target is not CaptureTarget.REGION):
        raise ProtocolError("'region' is required for, and only for, target region")
    raw_process = data.get("process")
    process = (
        None
        if raw_process is None or raw_process == ""
        else _extract_process_name(raw_process, "process")
    )
    raw_window_id = data.get("window_id")
    window_id = (
        None
        if raw_window_id is None or raw_window_id == ""
        else _extract_window_id(raw_window_id, "window_id")
    )
    return CaptureWindowPayload(
        target=target,
        process=process,
        title_contains=_extract_optional_request_text(
            data, "title_contains", CAPTURE_TITLE_MATCH_MAX_CHARS
        ),
        window_id=window_id,
        region=region,
        delay_ms=_extract_ranged_int(
            data, "delay_ms", 0, CAPTURE_DELAY_MAX_MS, default=0
        ),
        allowed=_extract_allowed(data),
        max_bytes=_extract_ranged_int(data, "max_bytes", 1, MANUAL_SHOT_MAX_BYTES),
        max_pixels=_extract_ranged_int(data, "max_pixels", 1, MANUAL_SHOT_MAX_PIXELS),
        switch_desktop=_extract_bool(dict(data), "switch_desktop", default=False),
    )


def serialize_capture_window_payload(
    payload: CaptureWindowPayload,
) -> dict[str, object]:
    """The JSON fields of a ``capture_window`` request (server side; passed
    as the action's keyword payload). Absent values are sent as null."""
    region: dict[str, object] | None = (
        None
        if payload.region is None
        else {
            "x": payload.region.x,
            "y": payload.region.y,
            "w": payload.region.w,
            "h": payload.region.h,
        }
    )
    return {
        "target": payload.target.value,
        "process": payload.process,
        "title_contains": payload.title_contains,
        "window_id": payload.window_id,
        "region": region,
        "delay_ms": payload.delay_ms,
        "allowed": list(payload.allowed),
        "max_bytes": payload.max_bytes,
        "max_pixels": payload.max_pixels,
        "switch_desktop": payload.switch_desktop,
    }


def _serialize_desktop(desktop: WindowDesktopPayload) -> dict[str, object]:
    return {
        "current": desktop.current,
        "index": desktop.index,
        "name": (
            None
            if desktop.name is None
            else desktop.name[:CAPTURE_DESKTOP_NAME_MAX_CHARS]
        ),
    }


def _parse_desktop(raw: object) -> WindowDesktopPayload:
    desktop = _extract_object(raw, "desktop")
    name = _extract_result_text(desktop, "name", CAPTURE_DESKTOP_NAME_MAX_CHARS)
    return WindowDesktopPayload(
        current=_extract_bool(desktop, "current"),
        index=_extract_optional_ranged_int(
            desktop, "index", 1, _CAPTURE_DESKTOP_INDEX_MAX
        ),
        name=name or None,
    )


def _parse_window_info(raw: object) -> WindowInfoPayload:
    window = _extract_object(raw, "window")
    limit = _CAPTURE_WINDOW_SIZE_MAX
    return WindowInfoPayload(
        window_id=_extract_window_id(window.get("window_id"), "window_id"),
        process=_extract_process_name(window.get("process"), "process"),
        title=_extract_result_text(window, "title", CAPTURE_TITLE_MAX_CHARS),
        width=_extract_ranged_int(window, "width", 0, limit, default=0),
        height=_extract_ranged_int(window, "height", 0, limit, default=0),
        minimized=_extract_bool(window, "minimized", default=False),
        focused=_extract_bool(window, "focused", default=False),
        desktop=_parse_desktop(window.get("desktop")),
    )


def serialize_window_list_result(result: WindowListResultPayload) -> dict[str, object]:
    """The ``result`` object of a ``window_list_result`` (runner side).

    Cuts titles, names and the detail to their caps and the list to
    WINDOW_LIST_MAX (the cut windows are added to ``others_hidden``)."""
    windows = result.windows[:WINDOW_LIST_MAX]
    return {
        "status": result.status.value,
        "detail": _clean_result_text(result.detail, CAPTURE_DETAIL_MAX_CHARS),
        "windows": [
            {
                "window_id": window.window_id,
                "process": window.process,
                "title": _clean_result_text(window.title, CAPTURE_TITLE_MAX_CHARS),
                "width": min(max(window.width, 0), _CAPTURE_WINDOW_SIZE_MAX),
                "height": min(max(window.height, 0), _CAPTURE_WINDOW_SIZE_MAX),
                "minimized": window.minimized,
                "focused": window.focused,
                "desktop": _serialize_desktop(window.desktop),
            }
            for window in windows
        ],
        "others_hidden": result.others_hidden + len(result.windows) - len(windows),
        "current_desktop": result.current_desktop,
        "desktop_count": result.desktop_count,
    }


def parse_window_list_result(raw: object) -> WindowListResultPayload:
    """Validate a ``window_list_result``'s ``result`` (server side).

    At most WINDOW_LIST_MAX windows, every process a valid normalized name;
    titles and names are cut, not refused. A non-OK status carries no
    windows (any sent are dropped). Raises ProtocolError otherwise."""
    data = _extract_object(raw, "window list result")
    status = _extract_status(data)
    raw_windows = _extract_list(data, "windows")
    if len(raw_windows) > WINDOW_LIST_MAX:
        raise ProtocolError(
            f"Invalid 'windows': expected at most {WINDOW_LIST_MAX} windows"
        )
    windows = (
        tuple(_parse_window_info(item) for item in raw_windows)
        if status is CaptureStatus.OK
        else ()
    )
    return WindowListResultPayload(
        status=status,
        detail=_extract_result_text(data, "detail", CAPTURE_DETAIL_MAX_CHARS),
        windows=windows,
        others_hidden=_extract_ranged_int(
            data, "others_hidden", 0, _CAPTURE_COUNT_MAX, default=0
        ),
        current_desktop=_extract_optional_ranged_int(
            data, "current_desktop", 1, _CAPTURE_DESKTOP_INDEX_MAX
        ),
        desktop_count=_extract_optional_ranged_int(
            data, "desktop_count", 1, _CAPTURE_DESKTOP_INDEX_MAX
        ),
    )


def serialize_window_capture_result(
    result: WindowCaptureResultPayload,
) -> dict[str, object]:
    """The ``result`` object of a ``window_capture_result`` (runner side).

    The image goes out base64 under ``data_b64`` only for an OK result; the
    caller keeps ``data`` within the request's ``max_bytes``."""
    has_image = result.status is CaptureStatus.OK and result.mime is not None
    return {
        "status": result.status.value,
        "detail": _clean_result_text(result.detail, CAPTURE_DETAIL_MAX_CHARS),
        "mime": result.mime.value if has_image and result.mime else None,
        "data_b64": (
            base64.b64encode(result.data).decode("ascii") if has_image else None
        ),
        "sha256": result.sha256 if has_image else "",
        "width": result.width if has_image else 0,
        "height": result.height if has_image else 0,
        "window_title": _clean_result_text(
            result.window_title, CAPTURE_TITLE_MAX_CHARS
        ),
        "process": result.process,
        "other_matches": result.other_matches,
        "desktop": (
            None if result.desktop is None else _serialize_desktop(result.desktop)
        ),
        "desktop_switched": result.desktop_switched,
        "desktop_restored": result.desktop_restored,
    }


def _parse_capture_image(
    data: Mapping[str, object], *, max_bytes: int
) -> tuple[ManualShotMime, bytes, str]:
    """The checked ``(mime, image, sha256)`` of an OK capture result."""
    raw_mime = data.get("mime")
    if not isinstance(raw_mime, str):
        raise ProtocolError("Missing or invalid 'mime'")
    try:
        mime = ManualShotMime(raw_mime)
    except ValueError as e:
        raise ProtocolError(f"Unsupported image type: {raw_mime[:40]!r}") from e
    raw_data = data.get("data_b64")
    if not isinstance(raw_data, str):
        raise ProtocolError("Missing or invalid 'data_b64'")
    # Checked before decoding, so an oversized result is never decoded.
    if len(raw_data) > 4 * -(-max_bytes // 3):
        raise ProtocolError("Screenshot too large")
    try:
        image = base64.b64decode(raw_data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ProtocolError("Invalid 'data_b64': not base64") from e
    if not 1 <= len(image) <= max_bytes:
        raise ProtocolError("Screenshot empty or too large")
    sha256 = _extract_sha256(data)
    if hashlib.sha256(image).hexdigest() != sha256:
        raise ProtocolError("Screenshot does not match its sha256")
    return mime, image, sha256


def parse_window_capture_result(
    raw: object, *, max_bytes: int
) -> WindowCaptureResultPayload:
    """Validate a ``window_capture_result``'s ``result`` (server side).

    For an OK result: decodes the base64 strictly and refuses (ProtocolError)
    an image over ``max_bytes`` (the request's), an empty one, one whose
    sha256 does not match, and sizes outside 1..CAPTURE_REGION_MAX_PIXELS_PER_AXIS;
    an OK result must name its ``process``. The image format itself is
    checked by the server's service (``check_manual_image``). A non-OK result
    never carries an image (any sent is ignored, not decoded).
    """
    data = _extract_object(raw, "window capture result")
    status = _extract_status(data)
    raw_process = data.get("process")
    process = (
        ""
        if raw_process is None or raw_process == ""
        else _extract_process_name(raw_process, "process")
    )
    mime: ManualShotMime | None = None
    image = b""
    sha256 = ""
    width = 0
    height = 0
    if status is CaptureStatus.OK:
        if not process:
            raise ProtocolError("Missing 'process' in an ok capture result")
        mime, image, sha256 = _parse_capture_image(data, max_bytes=max_bytes)
        limit = CAPTURE_REGION_MAX_PIXELS_PER_AXIS
        width = _extract_ranged_int(data, "width", 1, limit)
        height = _extract_ranged_int(data, "height", 1, limit)
    raw_desktop = data.get("desktop")
    return WindowCaptureResultPayload(
        status=status,
        detail=_extract_result_text(data, "detail", CAPTURE_DETAIL_MAX_CHARS),
        mime=mime,
        data=image,
        sha256=sha256,
        width=width,
        height=height,
        window_title=_extract_result_text(
            data, "window_title", CAPTURE_TITLE_MAX_CHARS
        ),
        process=process,
        other_matches=_extract_ranged_int(
            data, "other_matches", 0, _CAPTURE_COUNT_MAX, default=0
        ),
        desktop=None if raw_desktop is None else _parse_desktop(raw_desktop),
        desktop_switched=_extract_bool(dict(data), "desktop_switched", default=False),
        desktop_restored=_extract_optional_flag(data, "desktop_restored"),
    )


def serialize_window_list_result_message(
    result: WindowListResultPayload, request_id: str, duration_ms: float
) -> str:
    """A ``window_list_result`` message (runner side; echoes the request's
    ``request_id``)."""
    return serialize_message(
        MessageType.WINDOW_LIST_RESULT,
        request_id,
        success=True,
        result=serialize_window_list_result(result),
        duration_ms=duration_ms,
    )


def serialize_window_capture_result_message(
    result: WindowCaptureResultPayload, request_id: str, duration_ms: float
) -> str:
    """A ``window_capture_result`` message (runner side; echoes the request's
    ``request_id``)."""
    return serialize_message(
        MessageType.WINDOW_CAPTURE_RESULT,
        request_id,
        success=True,
        result=serialize_window_capture_result(result),
        duration_ms=duration_ms,
    )


# --- Installed apps of the phone (DEVICE_APPS_V1) ---------------------------
#
# The server asks a phone runner for the installed apps of the phone
# (``list_device_apps``, no fields besides ``request_id``). The runner answers
# ``device_apps_result`` with ``success: true`` and the result object (the
# ``serialize_device_apps_result`` JSON form) under ``result``; a refusal is a
# ``status`` other than ``ok`` inside that object, never an ``error`` message.
# The server re-validates every result with ``parse_device_apps_result``.

# Most apps one ``device_apps_result`` carries.
DEVICE_APPS_MAX: Final[int] = 1000
# Longest app display name a result carries (cut, not refused).
DEVICE_APP_NAME_MAX_CHARS: Final[int] = 64
# Highest ``skipped`` count a result may report.
_DEVICE_APPS_SKIPPED_MAX: Final[int] = 100_000
# Unicode categories dropped from an app name: control, format and lone
# surrogate characters.
_DEVICE_APP_NAME_DROPPED_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"Cc", "Cf", "Cs"}
)
# One JSON-escaped character at worst: a non-BMP code point is two
# ``\uXXXX`` escapes with ``json.dumps``' default ``ensure_ascii``.
_JSON_ESCAPED_CHAR_MAX_BYTES: Final[int] = 12
# Room for one app entry's keys, quotes and separators.
_DEVICE_APP_JSON_FIELDS_BYTES: Final[int] = 64
# Room for the rest of a ``device_apps_result`` besides the app list (the
# detail, the fixed fields and the message envelope, all JSON-escaped).
_DEVICE_APPS_JSON_OVERHEAD_BYTES: Final[int] = (
    _JSON_ESCAPED_CHAR_MAX_BYTES * CAPTURE_DETAIL_MAX_CHARS + 8 * 1024
)
# Largest raw ``device_apps_result`` message the server reads: every app at
# its longest id (ids are normalized process names, plain ASCII) and longest
# fully escaped name, plus the overhead.
DEVICE_APPS_RESULT_MAX_BYTES: Final[int] = (
    DEVICE_APPS_MAX
    * (
        CAPTURE_PROCESS_NAME_MAX_CHARS
        + _JSON_ESCAPED_CHAR_MAX_BYTES * DEVICE_APP_NAME_MAX_CHARS
        + _DEVICE_APP_JSON_FIELDS_BYTES
    )
    + _DEVICE_APPS_JSON_OVERHEAD_BYTES
)
assert (
    DEVICE_APPS_RESULT_MAX_BYTES <= MAX_MESSAGE_SIZE
), "DEVICE_APPS_RESULT_MAX_BYTES does not fit in MAX_MESSAGE_SIZE"


class DeviceAppsStatus(StrEnum):
    """Outcome of ``list_device_apps``; only ``ok`` carries apps."""

    OK = "ok"
    # The phone's driver cannot list the installed apps.
    UNSUPPORTED = "unsupported"
    # The phone is gone or still starting.
    DEVICE_UNAVAILABLE = "device_unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class DeviceAppPayload:
    # Normalized (``normalize_process_name``): the allowlist form of the app's
    # bundle id or package name.
    app_id: str
    # Display name, cut to DEVICE_APP_NAME_MAX_CHARS; "" when the platform
    # gives none. Data, never instructions.
    name: str


@dataclass(frozen=True)
class DeviceAppsResultPayload:
    """``device_apps_result``'s ``result``.

    ``apps``: at most DEVICE_APPS_MAX, ids unique; empty unless ``status`` is
    OK. ``names_available``: the platform reports display names (otherwise
    every ``name`` is ""). ``skipped``: apps left out because their id does
    not normalize. ``truncated``: apps were cut at DEVICE_APPS_MAX.
    ``detail``: plain text, at most CAPTURE_DETAIL_MAX_CHARS; the runner's
    fixed, user-safe explanation or "".
    """

    status: DeviceAppsStatus
    detail: str
    apps: tuple[DeviceAppPayload, ...]
    names_available: bool
    skipped: int
    truncated: bool


def _clean_device_app_name(value: str) -> str:
    """Without control, format and lone surrogate characters, stripped and
    cut to DEVICE_APP_NAME_MAX_CHARS."""
    cleaned = "".join(
        ch
        for ch in value
        if unicodedata.category(ch) not in _DEVICE_APP_NAME_DROPPED_CATEGORIES
    )
    return cleaned.strip()[:DEVICE_APP_NAME_MAX_CHARS]


def serialize_list_device_apps_payload() -> dict[str, object]:
    """The JSON fields of a ``list_device_apps`` request (server side; passed
    as the action's keyword payload): none besides ``request_id``."""
    return {}


def serialize_device_apps_result(result: DeviceAppsResultPayload) -> dict[str, object]:
    """The ``result`` object of a ``device_apps_result`` (runner side).

    Cuts names and the detail to their caps and the list to DEVICE_APPS_MAX
    (``truncated`` is then set). A non-OK status carries no apps."""
    apps = result.apps[:DEVICE_APPS_MAX] if result.status is DeviceAppsStatus.OK else ()
    return {
        "status": result.status.value,
        "detail": _clean_result_text(result.detail, CAPTURE_DETAIL_MAX_CHARS),
        "apps": [
            {"app_id": app.app_id, "name": _clean_device_app_name(app.name)}
            for app in apps
        ],
        "names_available": result.names_available,
        "skipped": min(max(result.skipped, 0), _DEVICE_APPS_SKIPPED_MAX),
        "truncated": result.truncated or len(result.apps) > DEVICE_APPS_MAX,
    }


def _parse_device_app(raw: object, seen: set[str]) -> DeviceAppPayload:
    app = _extract_object(raw, "app")
    app_id = app.get("app_id")
    if (
        not isinstance(app_id, str)
        or not app_id
        or len(app_id) > CAPTURE_PROCESS_NAME_MAX_CHARS
        or normalize_process_name(app_id) != app_id
    ):
        raise ProtocolError("Missing or invalid 'app_id': not a normalized app id")
    if app_id in seen:
        raise ProtocolError(f"Duplicate 'app_id': {app_id!r}")
    seen.add(app_id)
    name = app.get("name", "")
    if name is None:
        name = ""
    if not isinstance(name, str):
        raise ProtocolError("Invalid 'name': expected a string")
    return DeviceAppPayload(app_id=app_id, name=_clean_device_app_name(name))


def parse_device_apps_result(raw: object) -> DeviceAppsResultPayload:
    """Validate a ``device_apps_result``'s ``result`` (server side).

    At most DEVICE_APPS_MAX apps, every ``app_id`` already normalized
    (``normalize_process_name``) and unique; names lose control, format and
    lone surrogate characters and are cut, not refused. A non-OK status
    carries no apps (any sent are dropped). Raises ProtocolError otherwise."""
    data = _extract_object(raw, "device apps result")
    raw_status = data.get("status")
    if not isinstance(raw_status, str):
        raise ProtocolError("Missing or invalid 'status'")
    try:
        status = DeviceAppsStatus(raw_status)
    except ValueError as e:
        raise ProtocolError(f"Unknown device apps status: {raw_status[:40]!r}") from e
    raw_apps = _extract_list(data, "apps")
    if len(raw_apps) > DEVICE_APPS_MAX:
        raise ProtocolError(f"Invalid 'apps': expected at most {DEVICE_APPS_MAX} apps")
    seen: set[str] = set()
    apps = (
        tuple(_parse_device_app(item, seen) for item in raw_apps)
        if status is DeviceAppsStatus.OK
        else ()
    )
    return DeviceAppsResultPayload(
        status=status,
        detail=_extract_result_text(data, "detail", CAPTURE_DETAIL_MAX_CHARS),
        apps=apps,
        names_available=_extract_bool(data, "names_available", default=False),
        skipped=_extract_ranged_int(
            data, "skipped", 0, _DEVICE_APPS_SKIPPED_MAX, default=0
        ),
        truncated=_extract_bool(data, "truncated", default=False),
    )


def serialize_device_apps_result_message(
    result: DeviceAppsResultPayload, request_id: str, duration_ms: float
) -> str:
    """A ``device_apps_result`` message (runner side; echoes the request's
    ``request_id``)."""
    return serialize_message(
        MessageType.DEVICE_APPS_RESULT,
        request_id,
        success=True,
        result=serialize_device_apps_result(result),
        duration_ms=duration_ms,
    )


# --- Web browser layer (WEB_BROWSER_V1) -------------------------------------
#
# A WebDriver-based browser layer, separate from the Playwright ``browser_*``
# messages above (their ``BrowserName`` is untouched). The runner acts only on
# a browser that is already open and controllable; only ``web_browser_open``
# starts one. Every request except ``web_browser_open`` and
# ``web_browser_list`` carries an optional ``browser``: named -> that browser
# if open, else WEB_BROWSER_NOT_OPEN; not named -> the single open browser of
# the target's table, none -> WEB_BROWSER_NOT_OPEN, several ->
# WEB_BROWSER_AMBIGUOUS. Replies are ``web_browser_result``.


class WebBrowser(StrEnum):
    CHROME = "chrome"
    EDGE = "edge"
    FIREFOX = "firefox"
    SAFARI = "safari"


WEB_BROWSER_DISPLAY_NAMES: Final[dict[WebBrowser, str]] = {
    WebBrowser.CHROME: "Google Chrome",
    WebBrowser.EDGE: "Microsoft Edge",
    WebBrowser.FIREFOX: "Firefox",
    WebBrowser.SAFARI: "Safari",
}

# ``system_info.os`` values (``platform.system()``) of the desktop table.
_WEB_BROWSERS_DESKTOP: Final[dict[str, frozenset[WebBrowser]]] = {
    "Windows": frozenset({WebBrowser.CHROME, WebBrowser.EDGE, WebBrowser.FIREFOX}),
    "Linux": frozenset({WebBrowser.CHROME, WebBrowser.FIREFOX}),
    "Darwin": frozenset({WebBrowser.SAFARI, WebBrowser.CHROME, WebBrowser.FIREFOX}),
}
_WEB_BROWSERS_PHONE: Final[dict[RunnerTarget, frozenset[WebBrowser]]] = {
    RunnerTarget.ANDROID: frozenset(
        {WebBrowser.CHROME, WebBrowser.EDGE, WebBrowser.FIREFOX}
    ),
    RunnerTarget.IOS: frozenset(
        {WebBrowser.SAFARI, WebBrowser.CHROME, WebBrowser.EDGE, WebBrowser.FIREFOX}
    ),
}


def web_browsers_for(target: RunnerTarget, host_os: str) -> frozenset[WebBrowser]:
    """The one support table: the browsers a runner of ``target`` on
    ``host_os`` (``platform.system()``) serves. Unknown OS -> empty."""
    if target in _WEB_BROWSERS_PHONE:
        return _WEB_BROWSERS_PHONE[target]
    return _WEB_BROWSERS_DESKTOP.get(host_os, frozenset())


def ordered_web_browsers(browsers: frozenset[WebBrowser]) -> tuple[WebBrowser, ...]:
    """``browsers`` in enum order (stable lists for replies and errors)."""
    return tuple(browser for browser in WebBrowser if browser in browsers)


class WebBrowserMode(StrEnum):
    # WebDriver reaches the page (selectors, scripts, tabs).
    PAGE = "page"
    # Only the native accessibility tree of the web content (phones whose
    # browser exposes no inspectable page): list, tap, type, read.
    ACCESSIBILITY = "accessibility"


class WebBrowserHowToOpen(StrEnum):
    # A person may open it with the runner's test browser shortcut.
    SHORTCUT = "shortcut"
    # A person may open the app on the phone normally.
    APP = "app"
    # Only ``web_browser_open`` can open a controllable one.
    OPEN_TOOL_ONLY = "open_tool_only"


class WebBrowserSetupStep(StrEnum):
    """``details`` of WEB_BROWSER_SETUP_NEEDED: what the person must set up."""

    SAFARI_REMOTE_AUTOMATION_OFF = "safari_remote_automation_off"
    IOS_WEB_INSPECTOR_OFF = "ios_web_inspector_off"
    CHROME_IOS_WEB_INSPECTOR_OFF = "chrome_ios_web_inspector_off"
    DRIVER_UNAVAILABLE = "driver_unavailable"
    ANDROID_DEVTOOLS_UNAVAILABLE = "android_devtools_unavailable"
    FIREFOX_ANDROID_REMOTE_DEBUGGING_OFF = "firefox_android_remote_debugging_off"


class WebHistoryAction(StrEnum):
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"


class WebTabsAction(StrEnum):
    LIST = "list"
    SWITCH = "switch"
    CLOSE = "close"
    NEW = "new"


class WebActKind(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    HOVER = "hover"
    TYPE = "type"
    CLEAR = "clear"
    SELECT = "select"
    PRESS_KEY = "press_key"


class WebReadWhat(StrEnum):
    TEXT = "text"
    URL = "url"
    TITLE = "title"
    ELEMENT_TEXT = "element_text"
    ATTRIBUTE = "attribute"


class WebWaitState(StrEnum):
    PRESENT = "present"
    VISIBLE = "visible"
    GONE = "gone"


# Limits, checked by the parsers below (runner side) and mirrored by the
# server before it sends.
WEB_SELECTOR_MAX_CHARS: Final[int] = 2000
WEB_TYPE_TEXT_MAX_CHARS: Final[int] = 10_000
WEB_SELECT_VALUE_MAX_CHARS: Final[int] = 1000
WEB_KEY_MAX_CHARS: Final[int] = 64
WEB_ATTRIBUTE_NAME_MAX_CHARS: Final[int] = 128
WEB_SCRIPT_MAX_CHARS: Final[int] = 20_000
WEB_SCRIPT_ARGS_MAX_CHARS: Final[int] = 20_000
WEB_TIMEOUT_MS_MAX: Final[int] = 10_000
WEB_TIMEOUT_MS_DEFAULT: Final[int] = 5_000
WEB_ELEMENT_INDEX_MAX: Final[int] = 9_999
WEB_ELEMENTS_MAX: Final[int] = 300
WEB_ELEMENT_TEXT_MAX_CHARS: Final[int] = 100
WEB_TAB_INDEX_MAX: Final[int] = 99
WEB_READ_MAX_CHARS: Final[int] = 50_000
# Largest ``content`` a ``web_browser_result`` carries (the runner cuts and
# sets ``truncated``); evaluate results are cut at WEB_EVAL_RESULT_MAX_BYTES.
WEB_RESULT_CONTENT_MAX_CHARS: Final[int] = 60_000
WEB_EVAL_RESULT_MAX_BYTES: Final[int] = 64 * 1024
# Runner answer budgets: every reply comes within these, else a retryable
# error. ``web_browser_open`` may download a driver or attach to a phone.
WEB_BROWSER_ACTION_BUDGET_SECONDS: Final[int] = 24
# On a phone the call also waits for the phone session's own budget (24 s,
# after its presence check), so the hub allows a little more there.
WEB_BROWSER_PHONE_ACTION_BUDGET_SECONDS: Final[int] = 28
WEB_BROWSER_OPEN_BUDGET_SECONDS: Final[int] = 80
WEB_BROWSER_LIST_MAX: Final[int] = 8
WEB_BROWSER_VERSION_MAX_CHARS: Final[int] = 64

# URL schemes ``web_browser_open`` / ``web_browser_navigate`` accept. Scripts
# go through ``web_browser_evaluate``; ``file`` is refused on phones by the
# runner (it is desktop-only).
WEB_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "about", "file"})


@dataclass(frozen=True)
class WebElementTarget:
    """Exactly one of ``index`` (from the last element listing), ``css`` or
    ``xpath``. A plain-words description is resolved to an index on the
    server, never sent."""

    index: int | None = None
    css: str | None = None
    xpath: str | None = None


@dataclass(frozen=True)
class WebBrowserOpenPayload:
    browser: WebBrowser
    url: str | None


@dataclass(frozen=True)
class WebBrowserChoicePayload:
    """Requests that only pick a browser (``list`` ignores it, ``close``)."""

    browser: WebBrowser | None


@dataclass(frozen=True)
class WebBrowserNavigatePayload:
    browser: WebBrowser | None
    url: str


@dataclass(frozen=True)
class WebBrowserHistoryPayload:
    browser: WebBrowser | None
    action: WebHistoryAction


@dataclass(frozen=True)
class WebBrowserTabsPayload:
    """``index`` is required for ``switch``; optional for ``close`` (None:
    the current tab); ignored otherwise."""

    browser: WebBrowser | None
    action: WebTabsAction
    index: int | None


@dataclass(frozen=True)
class WebBrowserElementsPayload:
    browser: WebBrowser | None
    scope_css: str | None
    max_elements: int


@dataclass(frozen=True)
class WebBrowserActPayload:
    """``target`` is None only for ``press_key`` (the focused element).
    ``text`` for type, ``clear`` for type (clear first), ``value`` or
    ``label`` (exactly one) for select, ``key`` for press_key."""

    browser: WebBrowser | None
    action: WebActKind
    target: WebElementTarget | None
    text: str | None
    clear: bool
    value: str | None
    label: str | None
    key: str | None


@dataclass(frozen=True)
class WebBrowserReadPayload:
    """``target`` is required for element_text and attribute, ``name`` for
    attribute."""

    browser: WebBrowser | None
    what: WebReadWhat
    target: WebElementTarget | None
    name: str | None
    max_chars: int


@dataclass(frozen=True)
class WebBrowserWaitPayload:
    browser: WebBrowser | None
    target: WebElementTarget
    state: WebWaitState
    timeout_ms: int


@dataclass(frozen=True)
class WebBrowserEvaluatePayload:
    """``script`` is a function body run with ``arguments`` = the decoded
    ``args_json`` list."""

    browser: WebBrowser | None
    script: str
    args_json: str
    timeout_ms: int


@dataclass(frozen=True)
class WebBrowserListEntry:
    browser: WebBrowser
    installed: bool
    open: bool
    mode: WebBrowserMode | None
    how_to_open: WebBrowserHowToOpen
    version: str


@dataclass(frozen=True)
class WebBrowserResultPayload:
    """``web_browser_result``. ``content`` is text (page text, element list,
    JSON of an evaluate result or tab list, a short confirmation); ``browser``
    and ``mode`` name what the runner acted on (None for ``list``);
    ``browsers`` is set for ``list`` only."""

    success: bool
    content: str
    browser: WebBrowser | None
    mode: WebBrowserMode | None
    truncated: bool
    browsers: tuple[WebBrowserListEntry, ...]
    duration_ms: float


def _clean_wire_text(value: str) -> str:
    return "".join(ch for ch in value if ch.isprintable() or ch in "\n\t")


def _extract_optional_web_browser(data: Mapping[str, object]) -> WebBrowser | None:
    raw = data.get("browser")
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ProtocolError("Invalid 'browser'")
    try:
        return WebBrowser(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown browser: {raw}") from e


def _extract_bounded_str(
    data: Mapping[str, object], field: str, max_chars: int, *, required: bool
) -> str | None:
    value = data.get(field)
    if value is None:
        if required:
            raise ProtocolError(f"Missing '{field}'")
        return None
    if not isinstance(value, str) or len(value) > max_chars:
        raise ProtocolError(f"Invalid '{field}': not text or too long")
    if required and not value.strip():
        raise ProtocolError(f"Invalid '{field}': empty")
    return value


def validate_web_url(url: str) -> str:
    """``url`` stripped, or ProtocolError. Only WEB_URL_SCHEMES pass."""
    text = url.strip()
    if not text or len(text) > OPEN_URL_MAX_CHARS:
        raise ProtocolError("Invalid 'url': empty or too long")
    if any(not ch.isprintable() or ch.isspace() for ch in text):
        raise ProtocolError("Invalid 'url': contains spaces or control characters")
    match = _URL_SCHEME_RE.match(text)
    if match is None or match.group(1).lower() not in WEB_URL_SCHEMES:
        raise ProtocolError("Invalid 'url': only http, https, about and file URLs")
    return text


def web_url_is_file(url: str) -> bool:
    return url.strip().lower().startswith("file:")


def parse_web_element_target(raw: object) -> WebElementTarget:
    """Exactly one of ``index`` / ``css`` / ``xpath``."""
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid 'target': expected an object")
    index = raw.get("index")
    css = raw.get("css")
    xpath = raw.get("xpath")
    given = [value for value in (index, css, xpath) if value is not None]
    if len(given) != 1:
        raise ProtocolError("Invalid 'target': give exactly one of index, css, xpath")
    if index is not None:
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index <= WEB_ELEMENT_INDEX_MAX
        ):
            raise ProtocolError("Invalid 'target.index'")
        return WebElementTarget(index=index)
    selector = css if css is not None else xpath
    if (
        not isinstance(selector, str)
        or not selector.strip()
        or len(selector) > WEB_SELECTOR_MAX_CHARS
    ):
        raise ProtocolError("Invalid 'target' selector: empty or too long")
    if css is not None:
        return WebElementTarget(css=selector)
    return WebElementTarget(xpath=selector)


def serialize_web_element_target(target: WebElementTarget) -> dict[str, object]:
    if target.index is not None:
        return {"index": target.index}
    if target.css is not None:
        return {"css": target.css}
    return {"xpath": target.xpath}


def _optional_target(data: Mapping[str, object]) -> WebElementTarget | None:
    raw = data.get("target")
    return None if raw is None else parse_web_element_target(raw)


def _timeout_ms(data: Mapping[str, object]) -> int:
    value = data.get("timeout_ms", WEB_TIMEOUT_MS_DEFAULT)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError("Invalid 'timeout_ms'")
    return min(max(value, 0), WEB_TIMEOUT_MS_MAX)


def parse_web_browser_open_payload(data: dict[str, object]) -> WebBrowserOpenPayload:
    browser = _extract_optional_web_browser(data)
    if browser is None:
        raise ProtocolError("Missing 'browser'")
    raw_url = data.get("url")
    url = None
    if raw_url is not None and raw_url != "":
        if not isinstance(raw_url, str):
            raise ProtocolError("Invalid 'url'")
        url = validate_web_url(raw_url)
    return WebBrowserOpenPayload(browser=browser, url=url)


def parse_web_browser_choice_payload(
    data: dict[str, object],
) -> WebBrowserChoicePayload:
    return WebBrowserChoicePayload(browser=_extract_optional_web_browser(data))


def parse_web_browser_navigate_payload(
    data: dict[str, object],
) -> WebBrowserNavigatePayload:
    return WebBrowserNavigatePayload(
        browser=_extract_optional_web_browser(data),
        url=validate_web_url(_extract_str(data, "url")),
    )


def parse_web_browser_history_payload(
    data: dict[str, object],
) -> WebBrowserHistoryPayload:
    raw = _extract_str(data, "action")
    try:
        action = WebHistoryAction(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown history action: {raw}") from e
    return WebBrowserHistoryPayload(
        browser=_extract_optional_web_browser(data), action=action
    )


def parse_web_browser_tabs_payload(data: dict[str, object]) -> WebBrowserTabsPayload:
    raw = _extract_str(data, "action")
    try:
        action = WebTabsAction(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown tabs action: {raw}") from e
    index = _extract_optional_int(data, "index")
    if index is not None and not 0 <= index <= WEB_TAB_INDEX_MAX:
        raise ProtocolError("Invalid 'index'")
    if action is WebTabsAction.SWITCH and index is None:
        raise ProtocolError("Missing 'index' for switch")
    return WebBrowserTabsPayload(
        browser=_extract_optional_web_browser(data), action=action, index=index
    )


def parse_web_browser_elements_payload(
    data: dict[str, object],
) -> WebBrowserElementsPayload:
    scope = _extract_bounded_str(
        data, "scope_css", WEB_SELECTOR_MAX_CHARS, required=False
    )
    max_elements = _clamped_optional_int(data, "max", 1, WEB_ELEMENTS_MAX)
    return WebBrowserElementsPayload(
        browser=_extract_optional_web_browser(data),
        scope_css=scope.strip() if scope and scope.strip() else None,
        max_elements=WEB_ELEMENTS_MAX if max_elements is None else max_elements,
    )


def parse_web_browser_act_payload(data: dict[str, object]) -> WebBrowserActPayload:
    raw = _extract_str(data, "action")
    try:
        action = WebActKind(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown action: {raw}") from e
    target = _optional_target(data)
    if target is None and action is not WebActKind.PRESS_KEY:
        raise ProtocolError("Missing 'target'")
    text = _extract_bounded_str(data, "text", WEB_TYPE_TEXT_MAX_CHARS, required=False)
    # Typing only spaces is valid; only an empty text is refused.
    if action is WebActKind.TYPE and not text:
        raise ProtocolError("Missing 'text'")
    value = _extract_bounded_str(
        data, "value", WEB_SELECT_VALUE_MAX_CHARS, required=False
    )
    label = _extract_bounded_str(
        data, "label", WEB_SELECT_VALUE_MAX_CHARS, required=False
    )
    if action is WebActKind.SELECT and (value is None) == (label is None):
        raise ProtocolError("Give exactly one of 'value' or 'label' for select")
    key = _extract_bounded_str(
        data, "key", WEB_KEY_MAX_CHARS, required=action is WebActKind.PRESS_KEY
    )
    return WebBrowserActPayload(
        browser=_extract_optional_web_browser(data),
        action=action,
        target=target,
        text=text,
        clear=_extract_bool(data, "clear", default=False),
        value=value,
        label=label,
        key=key,
    )


def parse_web_browser_read_payload(data: dict[str, object]) -> WebBrowserReadPayload:
    raw = _extract_str(data, "what")
    try:
        what = WebReadWhat(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown read: {raw}") from e
    target = _optional_target(data)
    if target is None and what in (WebReadWhat.ELEMENT_TEXT, WebReadWhat.ATTRIBUTE):
        raise ProtocolError("Missing 'target'")
    name = _extract_bounded_str(
        data,
        "name",
        WEB_ATTRIBUTE_NAME_MAX_CHARS,
        required=what is WebReadWhat.ATTRIBUTE,
    )
    max_chars = _clamped_optional_int(data, "max_chars", 1, WEB_READ_MAX_CHARS)
    return WebBrowserReadPayload(
        browser=_extract_optional_web_browser(data),
        what=what,
        target=target,
        name=name,
        max_chars=WEB_READ_MAX_CHARS if max_chars is None else max_chars,
    )


def parse_web_browser_wait_payload(data: dict[str, object]) -> WebBrowserWaitPayload:
    target = _optional_target(data)
    if target is None:
        raise ProtocolError("Missing 'target'")
    raw = _extract_str(data, "state")
    try:
        state = WebWaitState(raw)
    except ValueError as e:
        raise ProtocolError(f"Unknown state: {raw}") from e
    return WebBrowserWaitPayload(
        browser=_extract_optional_web_browser(data),
        target=target,
        state=state,
        timeout_ms=_timeout_ms(data),
    )


def parse_web_browser_evaluate_payload(
    data: dict[str, object],
) -> WebBrowserEvaluatePayload:
    script = _extract_bounded_str(data, "script", WEB_SCRIPT_MAX_CHARS, required=True)
    args_json = _extract_bounded_str(
        data, "args_json", WEB_SCRIPT_ARGS_MAX_CHARS, required=False
    )
    args_text = args_json if args_json and args_json.strip() else "[]"
    try:
        decoded = json.loads(args_text)
    except json.JSONDecodeError as e:
        raise ProtocolError("Invalid 'args_json': not JSON") from e
    if not isinstance(decoded, list):
        raise ProtocolError("Invalid 'args_json': expected a JSON list")
    assert script is not None  # required=True
    return WebBrowserEvaluatePayload(
        browser=_extract_optional_web_browser(data),
        script=script,
        args_json=args_text,
        timeout_ms=_timeout_ms(data),
    )


def _serialize_list_entry(entry: WebBrowserListEntry) -> dict[str, object]:
    return {
        "browser": entry.browser.value,
        "installed": entry.installed,
        "open": entry.open,
        "mode": None if entry.mode is None else entry.mode.value,
        "how_to_open": entry.how_to_open.value,
        "version": entry.version[:WEB_BROWSER_VERSION_MAX_CHARS],
    }


def serialize_web_browser_result_message(
    result: WebBrowserResultPayload, request_id: str
) -> str:
    """A ``web_browser_result`` (runner side); ``content`` is cut to
    WEB_RESULT_CONTENT_MAX_CHARS (``truncated`` then set)."""
    content = result.content
    truncated = result.truncated or len(content) > WEB_RESULT_CONTENT_MAX_CHARS
    return serialize_message(
        MessageType.WEB_BROWSER_RESULT,
        request_id,
        success=result.success,
        content=content[:WEB_RESULT_CONTENT_MAX_CHARS],
        browser=None if result.browser is None else result.browser.value,
        mode=None if result.mode is None else result.mode.value,
        truncated=truncated,
        browsers=[
            _serialize_list_entry(entry)
            for entry in result.browsers[:WEB_BROWSER_LIST_MAX]
        ],
        duration_ms=result.duration_ms,
    )


def _parse_list_entry(raw: object) -> WebBrowserListEntry:
    if not isinstance(raw, dict):
        raise ProtocolError("Invalid 'browsers' entry")
    try:
        browser = WebBrowser(_extract_str(raw, "browser"))
        how_to_open = WebBrowserHowToOpen(_extract_str(raw, "how_to_open"))
        raw_mode = raw.get("mode")
        mode = None if raw_mode is None else WebBrowserMode(str(raw_mode))
    except ValueError as e:
        raise ProtocolError("Invalid 'browsers' entry") from e
    version = raw.get("version", "")
    if not isinstance(version, str):
        raise ProtocolError("Invalid 'browsers' entry version")
    return WebBrowserListEntry(
        browser=browser,
        installed=_extract_bool(raw, "installed"),
        open=_extract_bool(raw, "open"),
        mode=mode,
        how_to_open=how_to_open,
        version=_clean_wire_text(version)[:WEB_BROWSER_VERSION_MAX_CHARS],
    )


def parse_web_browser_result_payload(
    data: Mapping[str, object],
) -> WebBrowserResultPayload:
    """Validate a ``web_browser_result`` (server side). Unknown ``browser`` /
    ``mode`` values are refused; ``content`` is cleaned and capped."""
    content = data.get("content", "")
    if not isinstance(content, str):
        raise ProtocolError("Invalid 'content'")
    raw_browser = data.get("browser")
    raw_mode = data.get("mode")
    try:
        browser = None if raw_browser is None else WebBrowser(str(raw_browser))
        mode = None if raw_mode is None else WebBrowserMode(str(raw_mode))
    except ValueError as e:
        raise ProtocolError("Invalid 'browser' or 'mode'") from e
    raw_list = data.get("browsers", [])
    if not isinstance(raw_list, list) or len(raw_list) > WEB_BROWSER_LIST_MAX:
        raise ProtocolError("Invalid 'browsers'")
    success = data.get("success")
    truncated = data.get("truncated", False)
    duration = data.get("duration_ms", 0)
    if (
        not isinstance(success, bool)
        or not isinstance(truncated, bool)
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
    ):
        raise ProtocolError("Invalid 'success', 'truncated' or 'duration_ms'")
    over = len(content) > WEB_RESULT_CONTENT_MAX_CHARS
    return WebBrowserResultPayload(
        success=success,
        content=_clean_wire_text(content[:WEB_RESULT_CONTENT_MAX_CHARS]),
        browser=browser,
        mode=mode,
        truncated=truncated or over,
        browsers=tuple(_parse_list_entry(entry) for entry in raw_list),
        duration_ms=float(duration),
    )


def parse_web_browser_list_details(details: str) -> tuple[WebBrowser, ...]:
    """The ``details`` of WEB_BROWSER_AMBIGUOUS (comma-separated values) as
    WebBrowser values; unknown names are dropped, never echoed."""
    found: list[WebBrowser] = []
    for part in details.split(","):
        try:
            browser = WebBrowser(part.strip())
        except ValueError:
            continue
        if browser not in found:
            found.append(browser)
    return tuple(found)
