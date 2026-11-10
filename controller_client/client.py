import asyncio
import contextlib
import functools
import logging
import platform
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final, ParamSpec, TypeAlias, TypeVar

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from controller_client.app_checker import execute_check_app_installed
from controller_client.app_close_policy import (
    register_helper_pid_source,
    unregister_helper_pid_source,
)
from controller_client.app_closer import execute_close_app, execute_close_tab
from controller_client.app_launcher import execute_launch_app
from controller_client.artifact_scan import inspect_paths, scan_directory
from controller_client.browser_executor import (
    BrowserSession,
    execute_browser_click,
    execute_browser_double_click,
    execute_browser_download,
    execute_browser_get_elements,
    execute_browser_get_page_content,
    execute_browser_get_url,
    execute_browser_hover,
    execute_browser_list_downloads,
    execute_browser_navigate,
    execute_browser_take_screenshot,
    execute_browser_type,
)
from controller_client.cleanup import execute_cleanup
from controller_client.config import ClientConfig
from controller_client.device_apps import list_device_apps
from controller_client.device_apps import refusal as device_apps_refusal
from controller_client.exceptions import (
    AuthenticationError,
    BrowserElementNotFoundError,
    BrowserNotInstalledError,
    ConnectionTerminatedError,
    DeviceDisconnectedError,
    ExecutionError,
    HandshakeError,
    ProtocolError,
    TargetMismatchError,
    UnknownMessageTypeError,
    UpdateError,
    WebBrowserAmbiguousError,
    WebBrowserNotOpenError,
    WebBrowserSetupNeededError,
    WebBrowserUnsupportedError,
    WebScriptFailedError,
)
from controller_client.executor import (
    execute_click,
    execute_command_streaming,
    execute_double_click,
    execute_drag,
    execute_hover,
    execute_key_press,
    execute_screenshot,
    execute_scroll,
    execute_send_input,
    execute_start_interactive_cmd,
    execute_terminate_interactive_cmd,
    execute_type_text,
    execute_wait_for_command,
)
from controller_client.interactive_session import (
    InteractiveSessionManager,
    TooManySessionsError,
)
from controller_client.manual_capture import ManualCapturer
from controller_client.manual_coordinator import ManualCoordinator
from controller_client.manual_spool import ManualSpool
from controller_client.manual_uploader import ManualUploader
from controller_client.mobile_executor import (
    execute_device_check_app_installed,
    execute_device_cleanup,
    execute_device_close_app,
    execute_device_launch_app,
    execute_hide_keyboard,
    execute_install_app,
    execute_long_press,
    execute_open_url,
    execute_press_button,
    execute_rotate_device,
    execute_swipe,
)
from controller_client.person_window import (
    ManualWindowRequest,
    PersonCaseRequest,
    PersonWindow,
)
from controller_client.person_window_ipc import (
    CaseRequestKind,
    ManualCaseActionEvent,
    ManualChatSendEvent,
    ManualResultKind,
)
from controller_client.process_tracker import ProcessTracker
from controller_client.protocol import (
    CLIENT_VERSION,
    MAX_MESSAGE_SIZE,
    TERMINAL_CLOSE_CODES,
    UPDATE_STATUS_DETAIL_MAX_LENGTH,
    ActionResultPayload,
    BrowserContentResultPayload,
    CaptureStatus,
    CasePauseActionPayload,
    CloseCode,
    CommandResultPayload,
    DeviceAppsStatus,
    DeviceStatusPayload,
    ErrorCode,
    InteractiveOutputPayload,
    InteractiveSessionsPayload,
    ManualCaseActionKind,
    ManualCaseActionPayload,
    ManualChatSendPayload,
    MessageType,
    PersonChatSendPayload,
    PersonHelpActionPayload,
    PersonStepActionPayload,
    PersonStepActionResultPayload,
    ScreenshotResponsePayload,
    StreamName,
    UpdatePhase,
    advertised_capabilities,
    deserialize_server_message,
    inspect_paths_result_to_json,
    parse_browser_click_payload,
    parse_browser_download_payload,
    parse_browser_hover_payload,
    parse_browser_navigate_payload,
    parse_browser_target_payload,
    parse_browser_type_payload,
    parse_capture_window_payload,
    parse_case_pause_action_result_payload,
    parse_check_app_installed_payload,
    parse_click_payload,
    parse_close_app_payload,
    parse_close_tab_payload,
    parse_double_click_payload,
    parse_drag_payload,
    parse_handshake_ack_payload,
    parse_hover_payload,
    parse_inspect_paths_payload,
    parse_install_app_payload,
    parse_key_press_payload,
    parse_launch_app_payload,
    parse_list_windows_payload,
    parse_long_press_payload,
    parse_manual_case_action_result_payload,
    parse_manual_case_submit_result_payload,
    parse_manual_run_state_payload,
    parse_manual_screenshot_ack_payload,
    parse_open_url_payload,
    parse_person_case_action_result_payload,
    parse_person_step_action_result_payload,
    parse_person_step_state_payload,
    parse_press_button_payload,
    parse_rotate_device_payload,
    parse_run_command_payload,
    parse_scan_directory_payload,
    parse_scroll_payload,
    parse_send_input_payload,
    parse_start_interactive_cmd_payload,
    parse_swipe_payload,
    parse_terminate_interactive_cmd_payload,
    parse_type_text_payload,
    parse_wait_for_command_payload,
    peek_request_id,
    peek_update_id,
    scan_directory_result_to_json,
    serialize_case_pause_action,
    serialize_device_apps_result_message,
    serialize_device_status,
    serialize_interactive_sessions_message,
    serialize_manual_case_action,
    serialize_manual_chat_send,
    serialize_message,
    serialize_person_chat_send,
    serialize_person_help_action,
    serialize_person_step_action,
    serialize_web_browser_result_message,
    serialize_window_capture_result_message,
    serialize_window_list_result_message,
)
from controller_client.system_info import gather_system_info
from controller_client.target import current_target, get_mobile_device
from controller_client.update_layout import RUNNING_PACKAGE_DIR
from controller_client.update_runner import (
    RunnerUpdate,
    finish_update_as_new_runner,
    prepare_update,
)
from controller_client.web_browser_backend import WebBrowserContext
from controller_client.web_browser_hub import WebBrowserHub
from controller_client.window_capture import (
    WindowCaptureService,
    capture_refusal,
    list_refusal,
)

logger = logging.getLogger(__name__)

# How long the server may take to answer the handshake before we drop the
# socket and try again; without it a server that accepts but never acks would
# leave the client waiting forever.
HANDSHAKE_ACK_TIMEOUT_SECONDS: Final[float] = 30.0
OPEN_TIMEOUT_SECONDS: Final[float] = 15.0
CLOSE_TIMEOUT_SECONDS: Final[float] = 10.0
PING_INTERVAL_SECONDS: Final[float] = 30.0
PING_TIMEOUT_SECONDS: Final[float] = 120.0
# Ack statuses that retrying cannot fix: a wrong key or an outdated client.
_TERMINAL_HANDSHAKE_STATUSES: Final[frozenset[str]] = frozenset(
    {"error", "incompatible"}
)
# Keeps the backoff exponent small; the delay is capped long before this.
_MAX_BACKOFF_EXPONENT: Final[int] = 16
# How long a person's answer from the window waits for the server's result
# before the window gets its buttons back.
PERSON_STEP_ANSWER_TIMEOUT_SECONDS: Final[float] = 30.0
# The same for a pause, a discussion message or a help resolution from the
# window (one slot per kind).
PERSON_CASE_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
# The same for a manual claim, release, pause or continue (one at a time), and
# for a manual discussion message (its own slot).
MANUAL_CASE_ACTION_TIMEOUT_SECONDS: Final[float] = 30.0
_MIB: Final[int] = 1024 * 1024
# Messages that never touch or capture the screen, so the person step window
# stays up while they are handled. Every other message is an agent action and
# runs with the window hidden (it must never be in a screenshot, the vision
# parse or under a click).
_WINDOW_SAFE_MESSAGE_TYPES: Final[frozenset[MessageType]] = frozenset(
    {
        MessageType.PING,
        MessageType.PERSON_STEP_STATE,
        MessageType.PERSON_STEP_ACTION_RESULT,
        MessageType.CASE_PAUSE_ACTION_RESULT,
        MessageType.PERSON_CASE_ACTION_RESULT,
        # Manual run: snapshots and replies; tester screenshots hide the
        # window themselves (manual_capture).
        MessageType.MANUAL_RUN_STATE,
        MessageType.MANUAL_CASE_ACTION_RESULT,
        MessageType.MANUAL_CASE_SUBMIT_RESULT,
        MessageType.MANUAL_SCREENSHOT_ACK,
        MessageType.UPDATE_RUNNER,
        # Read-only file system inventory; nothing on screen changes.
        MessageType.SCAN_DIRECTORY,
        MessageType.INSPECT_PATHS,
        # Window capture: list_windows only reads the window list, and
        # capture_window hides the window itself with the confirmed variant
        # (window_capture), like tester screenshots.
        MessageType.LIST_WINDOWS,
        MessageType.CAPTURE_WINDOW,
        # Lists the runner's shell sessions; touches no screen.
        MessageType.LIST_INTERACTIVE_SESSIONS,
        # Reads the phone's installed apps; nothing on screen is read.
        MessageType.LIST_DEVICE_APPS,
    }
)
# Desktop-only messages, answered TARGET_MISMATCH on a phone target: there is
# no hover on a phone, and the DOM browser tools drive a host browser (Q2).
_DESKTOP_ONLY_MESSAGE_TYPES: Final[frozenset[MessageType]] = frozenset(
    {
        MessageType.HOVER,
        MessageType.CLOSE_TAB,
        MessageType.BROWSER_NAVIGATE,
        MessageType.BROWSER_CLICK,
        MessageType.BROWSER_DOUBLE_CLICK,
        MessageType.BROWSER_TYPE,
        MessageType.BROWSER_HOVER,
        MessageType.BROWSER_GET_ELEMENTS,
        MessageType.BROWSER_GET_PAGE_CONTENT,
        MessageType.BROWSER_GET_URL,
        MessageType.BROWSER_TAKE_SCREENSHOT,
        MessageType.BROWSER_DOWNLOAD,
        MessageType.BROWSER_LIST_DOWNLOADS,
    }
)
# Phone-only messages (MOBILE_DEVICE_V1), answered TARGET_MISMATCH on a
# desktop target.
_PHONE_ONLY_MESSAGE_TYPES: Final[frozenset[MessageType]] = frozenset(
    {
        MessageType.SWIPE,
        MessageType.LONG_PRESS,
        MessageType.PRESS_BUTTON,
        MessageType.ROTATE_DEVICE,
        MessageType.HIDE_KEYBOARD,
        MessageType.OPEN_URL,
        MessageType.INSTALL_APP,
        MessageType.LIST_DEVICE_APPS,
    }
)
_ANSWER_NO_REPLY: Final[str] = "No answer from the server; check Punk Hazard."
_ANSWER_CONNECTION_LOST: Final[str] = (
    "The connection to Punk Hazard was lost before it answered; check Punk "
    "Hazard whether your answer was saved."
)
_ANSWER_NOT_CONNECTED: Final[str] = (
    "Not connected to Punk Hazard; answer again once the Test Runner reconnects."
)
_ANSWER_BUSY: Final[str] = "Your previous answer is still being sent; wait for it."
_ANSWER_UNREADABLE: Final[str] = (
    "The server's reply could not be read; check Punk Hazard."
)
_MANUAL_ACTION_NOUNS: Final[dict[ManualCaseActionKind, str]] = {
    ManualCaseActionKind.CLAIM: "start",
    ManualCaseActionKind.RELEASE: "release",
    ManualCaseActionKind.PAUSE: "pause",
    ManualCaseActionKind.CONTINUE: "continue",
}
_MANUAL_ACTION_KINDS: Final[dict[ManualCaseActionKind, ManualResultKind]] = {
    ManualCaseActionKind.CLAIM: ManualResultKind.CLAIM,
    ManualCaseActionKind.RELEASE: ManualResultKind.RELEASE,
    ManualCaseActionKind.PAUSE: ManualResultKind.PAUSE,
    ManualCaseActionKind.CONTINUE: ManualResultKind.CONTINUE,
}
# Sent right away; pause, continue and discussion messages queue behind the
# window's earlier draft changes so the marks reported first are the newest.
_MANUAL_CLAIM_ACTIONS: Final[frozenset[ManualCaseActionKind]] = frozenset(
    {ManualCaseActionKind.CLAIM, ManualCaseActionKind.RELEASE}
)
_MANUAL_CHAT_NOT_YOURS: Final[str] = (
    "This case is no longer yours; your message was not sent."
)
_MANUAL_CHAT_CONNECTION_LOST: Final[str] = (
    "The connection to Punk Hazard was lost before it answered; check the "
    "discussion whether your message arrived."
)
_UPDATE_SPOOL_NOT_EMPTY: Final[str] = (
    "Finish uploading the manual test screenshots first: the Test Runner "
    "still holds {spool}"
)
# What each case request is called in the window's messages.
_CASE_REQUEST_NOUNS: Final[dict[CaseRequestKind, str]] = {
    CaseRequestKind.PAUSE: "pause",
    CaseRequestKind.CHAT: "message",
    CaseRequestKind.HELP: "decision",
}

MessageHandler: TypeAlias = Callable[
    [str, dict[str, object]], Coroutine[Any, Any, None]
]

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _answer_failed(message: str) -> PersonStepActionResultPayload:
    return PersonStepActionResultPayload(ok=False, message=message, queued=False)


def _case_request_kind(request: PersonCaseRequest) -> CaseRequestKind:
    match request:
        case CasePauseActionPayload():
            return CaseRequestKind.PAUSE
        case PersonChatSendPayload():
            return CaseRequestKind.CHAT
        case PersonHelpActionPayload():
            return CaseRequestKind.HELP


def _serialize_case_request(request: PersonCaseRequest, request_id: str) -> str:
    match request:
        case CasePauseActionPayload():
            return serialize_case_pause_action(request, request_id)
        case PersonChatSendPayload():
            return serialize_person_chat_send(request, request_id)
        case PersonHelpActionPayload():
            return serialize_person_help_action(request, request_id)


def _error_code(exc: Exception, fallback: ErrorCode) -> ErrorCode:
    """Map an action failure to the error code the server acts on."""
    if isinstance(exc, DeviceDisconnectedError):
        return ErrorCode.DEVICE_DISCONNECTED
    if isinstance(exc, TargetMismatchError):
        return ErrorCode.TARGET_MISMATCH
    if isinstance(exc, TooManySessionsError):
        return ErrorCode.TOO_MANY_SESSIONS
    return fallback


# The web browser layer (WEB_BROWSER_V1): every message goes to the hub.
_WEB_BROWSER_MESSAGE_TYPES: Final[frozenset[MessageType]] = frozenset(
    {
        MessageType.WEB_BROWSER_OPEN,
        MessageType.WEB_BROWSER_LIST,
        MessageType.WEB_BROWSER_NAVIGATE,
        MessageType.WEB_BROWSER_HISTORY,
        MessageType.WEB_BROWSER_TABS,
        MessageType.WEB_BROWSER_ELEMENTS,
        MessageType.WEB_BROWSER_ACT,
        MessageType.WEB_BROWSER_READ,
        MessageType.WEB_BROWSER_WAIT,
        MessageType.WEB_BROWSER_EVALUATE,
        MessageType.WEB_BROWSER_CLOSE,
    }
)


def _web_browser_error(exc: Exception) -> tuple[ErrorCode, str]:
    """The error code and ``details`` for a web browser layer failure."""
    if isinstance(exc, WebBrowserSetupNeededError):
        return ErrorCode.WEB_BROWSER_SETUP_NEEDED, exc.step
    if isinstance(exc, WebBrowserUnsupportedError):
        return ErrorCode.WEB_BROWSER_UNSUPPORTED, ""
    if isinstance(exc, WebBrowserNotOpenError):
        return ErrorCode.WEB_BROWSER_NOT_OPEN, exc.named
    if isinstance(exc, WebBrowserAmbiguousError):
        return ErrorCode.WEB_BROWSER_AMBIGUOUS, ",".join(exc.open_browsers)
    if isinstance(exc, WebScriptFailedError):
        return ErrorCode.SCRIPT_FAILED, exc.error_class
    return _error_code(exc, _browser_error_code(exc, ErrorCode.EXECUTION_FAILED)), ""


def _target_mismatch(message_type: MessageType) -> str | None:
    """Why ``message_type`` cannot run on this runner's target, or None."""
    on_phone = get_mobile_device() is not None
    if on_phone and message_type in _DESKTOP_ONLY_MESSAGE_TYPES:
        return f"{message_type.value} is not available on a phone or tablet target"
    if not on_phone and message_type in _PHONE_ONLY_MESSAGE_TYPES:
        return f"{message_type.value} needs a phone or tablet target"
    return None


def _browser_error_code(exc: Exception, fallback: ErrorCode) -> ErrorCode:
    """Map a browser action failure to the error code the server acts on."""
    if isinstance(exc, BrowserNotInstalledError):
        return ErrorCode.BROWSER_NOT_INSTALLED
    if isinstance(exc, BrowserElementNotFoundError):
        return ErrorCode.ELEMENT_NOT_FOUND
    return fallback


class ControllerClient:
    def __init__(
        self,
        config: ClientConfig,
        interactive_cmd_timeout: float = 300.0,
        person_window: PersonWindow | None = None,
    ) -> None:
        self._config = config
        self._running = False
        self._connection: ClientConnection | None = None
        self._browser_session = BrowserSession()
        # Playwright's sync API is bound to the thread that started it, so every
        # browser call goes through this one worker instead of asyncio.to_thread.
        self._browser_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="playwright"
        )
        self._session_manager = InteractiveSessionManager()
        self._process_tracker = ProcessTracker()
        # The web browser layer; built at the first web_browser_* message,
        # once the target backend (and its phone) is selected.
        self._web_browser_hub: WebBrowserHub | None = None
        self._interactive_cmd_timeout = interactive_cmd_timeout
        self._handlers: dict[MessageType, MessageHandler] = {
            MessageType.CLICK: self._handle_click,
            MessageType.DOUBLE_CLICK: self._handle_double_click,
            MessageType.HOVER: self._handle_hover,
            MessageType.DRAG: self._handle_drag,
            MessageType.SCROLL: self._handle_scroll,
            MessageType.TYPE_TEXT: self._handle_type_text,
            MessageType.KEY_PRESS: self._handle_key_press,
            MessageType.SCREENSHOT_REQUEST: self._handle_screenshot,
            MessageType.RUN_COMMAND: self._handle_run_command,
            MessageType.PING: self._handle_ping,
            MessageType.BROWSER_NAVIGATE: self._handle_browser_navigate,
            MessageType.BROWSER_CLICK: self._handle_browser_click,
            MessageType.BROWSER_DOUBLE_CLICK: self._handle_browser_double_click,
            MessageType.BROWSER_TYPE: self._handle_browser_type,
            MessageType.BROWSER_HOVER: self._handle_browser_hover,
            MessageType.BROWSER_GET_ELEMENTS: self._handle_browser_get_elements,
            MessageType.BROWSER_GET_PAGE_CONTENT: self._handle_browser_get_page_content,
            MessageType.BROWSER_GET_URL: self._handle_browser_get_url,
            MessageType.BROWSER_TAKE_SCREENSHOT: self._handle_browser_take_screenshot,
            MessageType.BROWSER_DOWNLOAD: self._handle_browser_download,
            MessageType.BROWSER_LIST_DOWNLOADS: self._handle_browser_list_downloads,
            MessageType.START_INTERACTIVE_CMD: self._handle_start_interactive_cmd,
            MessageType.SEND_INPUT: self._handle_send_input,
            MessageType.WAIT_FOR_COMMAND: self._handle_wait_for_command,
            MessageType.TERMINATE_INTERACTIVE_CMD: self._handle_terminate_interactive_cmd,
            MessageType.LIST_INTERACTIVE_SESSIONS: (
                self._handle_list_interactive_sessions
            ),
            MessageType.LAUNCH_APP: self._handle_launch_app,
            MessageType.CLOSE_APP: self._handle_close_app,
            MessageType.CLOSE_TAB: self._handle_close_tab,
            MessageType.CHECK_APP_INSTALLED: self._handle_check_app_installed,
            MessageType.CLEANUP_ENVIRONMENT: self._handle_cleanup_environment,
            MessageType.SCAN_DIRECTORY: self._handle_scan_directory,
            MessageType.INSPECT_PATHS: self._handle_inspect_paths,
            # Phone and tablet target (MOBILE_DEVICE_V1).
            MessageType.SWIPE: self._handle_swipe,
            MessageType.LONG_PRESS: self._handle_long_press,
            MessageType.PRESS_BUTTON: self._handle_press_button,
            MessageType.ROTATE_DEVICE: self._handle_rotate_device,
            MessageType.HIDE_KEYBOARD: self._handle_hide_keyboard,
            MessageType.OPEN_URL: self._handle_open_url,
            MessageType.INSTALL_APP: self._handle_install_app,
            MessageType.LIST_DEVICE_APPS: self._handle_list_device_apps,
            # Window capture (WINDOW_CAPTURE_V1), every target.
            MessageType.LIST_WINDOWS: self._handle_list_windows,
            MessageType.CAPTURE_WINDOW: self._handle_capture_window,
        }
        for web_type in _WEB_BROWSER_MESSAGE_TYPES:
            self._handlers[web_type] = functools.partial(
                self._handle_web_browser, web_type
            )
        # The latest device_status, re-sent after every accepted handshake.
        self._last_device_status: DeviceStatusPayload | None = None
        # Self-update (SELF_UPDATE_V1).
        self._handlers[MessageType.UPDATE_RUNNER] = self._handle_update_runner
        # The accepted update (its workflow lives in update_runner) and the
        # task running it.
        self._update: RunnerUpdate | None = None
        self._update_task: asyncio.Task[None] | None = None
        # Last update_status (update_id, phase, detail), re-sent after a
        # reconnect while the update runs or when its send failed.
        self._last_update_status: tuple[str, UpdatePhase, str] | None = None
        self._update_status_unsent = False
        self._handed_over = False
        # The new runner's post-handshake work (marker, start script, removal).
        self._update_finish_task: asyncio.Task[None] | None = None
        # Set by stop() so a pending reconnect backoff ends immediately.
        self._stop_event = asyncio.Event()
        # Whether the current session got an "ok" handshake ack; run() resets
        # the backoff only after such a session.
        self._session_authenticated = False
        # This runner's name: CONTROLLER_RUNNER_NAME until a handshake ack
        # names it (display only: logs and the window title).
        self._runner_name = config.runner_name
        # Person steps (PERSON_STEPS_V1): the window, when enabled and Tk
        # works; None advertises no capability and the server sends nothing.
        self._window = person_window
        self._handlers[MessageType.PERSON_STEP_STATE] = self._handle_person_step_state
        self._handlers[MessageType.PERSON_STEP_ACTION_RESULT] = (
            self._handle_person_step_action_result
        )
        self._handlers[MessageType.CASE_PAUSE_ACTION_RESULT] = (
            self._handle_case_pause_action_result
        )
        self._handlers[MessageType.PERSON_CASE_ACTION_RESULT] = (
            self._handle_person_case_action_result
        )
        # Forwards the window's answers to the server (started by run()).
        self._answer_task: asyncio.Task[None] | None = None
        # The one answer waiting for its result: (request_id, its timeout).
        self._pending_answer: tuple[str, asyncio.Task[None]] | None = None
        # Window v2: forwards pauses, discussion messages and help
        # resolutions; one waits for its result per kind.
        self._case_request_task: asyncio.Task[None] | None = None
        self._pending_case: dict[CaseRequestKind, tuple[str, asyncio.Task[None]]] = {}
        # Manual run (MANUAL_RUN_V1). The spool and its uploader work with or
        # without the window (hand-ins of an earlier session still upload);
        # the coordinator and captures need the window.
        self._manual_spool = ManualSpool(max_bytes=config.manual_spool_max_mb * _MIB)
        self._manual_uploader = ManualUploader(
            self._manual_spool, on_change=self._on_manual_upload_change
        )
        self._manual_upload_task: asyncio.Task[None] | None = None
        self._manual: ManualCoordinator | None = None
        self._manual_request_task: asyncio.Task[None] | None = None
        # The one claim or release waiting for its result:
        # (request_id, action, its timeout).
        self._pending_manual_action: (
            tuple[str, ManualCaseActionPayload, asyncio.Task[None]] | None
        ) = None
        # A manual discussion message (MANUAL_PAUSE_DISCUSS_V1), its own slot;
        # answered by manual_case_action_result with its request_id.
        self._pending_manual_chat: tuple[str, asyncio.Task[None]] | None = None
        self._handlers[MessageType.MANUAL_RUN_STATE] = self._handle_manual_run_state
        self._handlers[MessageType.MANUAL_CASE_ACTION_RESULT] = (
            self._handle_manual_case_action_result
        )
        self._handlers[MessageType.MANUAL_CASE_SUBMIT_RESULT] = (
            self._handle_manual_case_submit_result
        )
        self._handlers[MessageType.MANUAL_SCREENSHOT_ACK] = (
            self._handle_manual_screenshot_ack
        )
        # list_windows / capture_window; hides the person window itself.
        self._window_capture = WindowCaptureService(person_window)
        self._window_pid_source: Callable[[], int | None] | None = None
        if person_window is not None:
            self._window_pid_source = lambda: person_window.pid
            # close_app / close_tab never close the window process.
            register_helper_pid_source(self._window_pid_source)
            self._manual = ManualCoordinator(
                window=person_window,
                spool=self._manual_spool,
                uploader=self._manual_uploader,
                capturer=ManualCapturer(person_window),
                send=self._send_manual_upload,
            )

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        attempt = 0
        window = self._window
        if window is not None and self._answer_task is None:
            self._answer_task = asyncio.create_task(self._forward_answers(window))
        if window is not None and self._case_request_task is None:
            self._case_request_task = asyncio.create_task(
                self._forward_case_requests(window)
            )
        if window is not None and self._manual_request_task is None:
            self._manual_request_task = asyncio.create_task(
                self._forward_manual_requests(window)
            )
        if self._manual_upload_task is None:
            await self._recover_manual_spool()
            self._manual_upload_task = asyncio.create_task(self._manual_uploader.run())

        while self._running:
            self._session_authenticated = False
            try:
                await self._connect_and_listen()
                reason = "connection closed"
            except AuthenticationError as e:
                logger.error("Authentication failed: %s", e)
                break
            except ConnectionTerminatedError as e:
                logger.error("%s. Not reconnecting.", e)
                self._log_handover_if_superseded(e.code)
                break
            except Exception as e:
                reason = str(e) or type(e).__name__

            if not self._running:
                break
            if await self._handover_ends_session():
                break
            if self._session_authenticated:
                attempt = 0
            attempt += 1
            max_attempts = self._config.max_reconnect_attempts
            if max_attempts > 0 and attempt > max_attempts:
                logger.error(
                    "Connection lost (%s). Max reconnect attempts (%d) reached. "
                    "Shutting down.",
                    reason,
                    max_attempts,
                )
                break

            delay = self._reconnect_delay(attempt)
            logger.warning(
                "Connection lost (%s). Reconnect attempt %d%s in %ds...",
                reason,
                attempt,
                f"/{max_attempts}" if max_attempts > 0 else "",
                delay,
            )
            await self._sleep_unless_stopped(delay)

        self._running = False

    def _reconnect_delay(self, attempt: int) -> int:
        """Exponential backoff from reconnect_interval, capped at reconnect_max_interval."""
        exponent = min(attempt - 1, _MAX_BACKOFF_EXPONENT)
        return min(
            self._config.reconnect_interval << exponent,
            self._config.reconnect_max_interval,
        )

    async def _sleep_unless_stopped(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _run_browser(
        self, fn: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs
    ) -> _R:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._browser_executor, functools.partial(fn, *args, **kwargs)
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        # A capture that switched desktops switches back on its worker thread;
        # wait for it before the person window and the loop go away.
        await self._window_capture.wait_idle()
        await self._cancel_update_tasks()
        await self._stop_manual_upload()
        await self._stop_person_window()
        self._process_tracker.kill_all()
        self._session_manager.terminate_all()
        # The executor is not shut down: stop() can run twice (signal, then
        # after run() returns) and the idle worker is joined at exit anyway.
        await self._run_browser(self._browser_session.close)
        if self._web_browser_hub is not None:
            await self._web_browser_hub.shutdown()
        if self._connection is not None:
            await self._connection.close()

    async def _connect_and_listen(self) -> None:
        url = self._config.ws_url
        logger.info("Connecting to %s", url)

        # Client keepalive matches the tolerance Daphne already applies to us
        # (--ping-interval 30 --ping-timeout 120): frames are ordered, so on a
        # slow uplink a pong can sit behind a multi-MiB screenshot for a
        # long time. The library's 20s default would then close a healthy
        # socket (1011 "keepalive ping timeout"); 120s does not, yet still
        # detects a half-open link within ~150s instead of waiting on it
        # forever.
        session_error: ConnectionClosed | None = None
        async with websockets.connect(
            url,
            max_size=MAX_MESSAGE_SIZE,
            open_timeout=OPEN_TIMEOUT_SECONDS,
            close_timeout=CLOSE_TIMEOUT_SECONDS,
            ping_interval=PING_INTERVAL_SECONDS,
            ping_timeout=PING_TIMEOUT_SECONDS,
            # True = the system proxy settings (HTTPS_PROXY, ...).
            proxy=self._config.proxy or True,
        ) as connection:
            self._connection = connection
            try:
                await self._perform_handshake(connection)
                if self._window is not None:
                    self._window.set_connected(True, runner_name=self._runner_name)
                await self._after_handshake()
                await self._message_loop(connection)
            except ConnectionClosed as e:
                session_error = e
            finally:
                self._connection = None
                self._on_session_ended()

        # Leaving the context manager closed the socket, so close_code is set.
        self._raise_if_terminated(connection.close_code)
        if session_error is not None:
            raise session_error

    @staticmethod
    def _raise_if_terminated(close_code: int | None) -> None:
        """Turn a close code the server uses to end a session for good into a stop."""
        if close_code not in TERMINAL_CLOSE_CODES:
            return
        if close_code == CloseCode.SUPERSEDED:
            logger.error("This Test Runner was replaced by another Test Runner")
        else:
            logger.error("This Test Runner was disconnected from the panel")
        raise ConnectionTerminatedError(close_code)

    async def _perform_handshake(self, connection: ClientConnection) -> None:
        """Send the handshake and wait for its ack before handling any command.

        An "error" or "incompatible" ack raises AuthenticationError, which stops
        the client; anything else that is not an "ok" ack, or no ack in time,
        raises a retryable error.
        """
        await self._send_handshake(connection)
        try:
            raw_ack = await asyncio.wait_for(
                connection.recv(), timeout=HANDSHAKE_ACK_TIMEOUT_SECONDS
            )
        except TimeoutError as e:
            raise HandshakeError(
                f"No handshake ack within {HANDSHAKE_ACK_TIMEOUT_SECONDS:.0f}s"
            ) from e
        if not isinstance(raw_ack, str):
            raise HandshakeError("Handshake ack was not a text message")

        try:
            message_type, _, data = deserialize_server_message(raw_ack)
            if message_type is not MessageType.HANDSHAKE_ACK:
                raise ProtocolError(f"expected handshake_ack, got {message_type}")
            ack = parse_handshake_ack_payload(data)
        except ProtocolError as e:
            raise HandshakeError(f"Invalid handshake ack: {e}") from e

        if ack.status in _TERMINAL_HANDSHAKE_STATUSES:
            raise AuthenticationError(
                f"Handshake rejected ({ack.status}): {ack.message}"
            )
        if ack.status != "ok":
            raise HandshakeError(
                f"Handshake not accepted ({ack.status}): {ack.message}"
            )

        if ack.runner_name:
            self._runner_name = ack.runner_name
        logger.info(
            "Connected as Test Runner '%s' (id=%s)",
            self._runner_name or "unnamed",
            ack.runner_id,
        )
        self._session_authenticated = True

    async def _send_handshake(self, connection: ClientConnection) -> None:
        system_info = await asyncio.to_thread(gather_system_info)
        payload: dict[str, object] = {
            # Never logged: it authenticates this runner.
            "runner_token": self._config.runner_token,
            "client_version": CLIENT_VERSION,
            "capabilities": [
                capability.value
                for capability in advertised_capabilities(
                    person_window=self._window is not None,
                    mobile=get_mobile_device() is not None,
                )
            ],
            "system_info": system_info.to_dict(),
        }
        update_id = self._update_id_to_keep()
        if update_id is not None:
            # Reconnected mid-update, or with its final status still unsent:
            # lets the server keep the update row until _after_handshake
            # re-sends that status (a terminal one keeps its real detail).
            payload["update_in_flight_id"] = update_id
        message = serialize_message(MessageType.HANDSHAKE, None, **payload)
        await connection.send(message)
        logger.info("Handshake sent (client_version=%s)", CLIENT_VERSION)

    async def _message_loop(self, connection: ClientConnection) -> None:
        closed = asyncio.create_task(connection.wait_closed())
        try:
            await self._receive_messages(connection, closed)
        finally:
            closed.cancel()

    async def _receive_messages(
        self, connection: ClientConnection, closed: asyncio.Task[None]
    ) -> None:
        async for raw_message in connection:
            if not self._running:
                break

            if not isinstance(raw_message, str):
                logger.warning("Received non-text message, ignoring")
                continue

            try:
                message_type, request_id, data = deserialize_server_message(raw_message)
            except ProtocolError as e:
                await self._reject_undecodable_message(raw_message, e)
                continue

            handler = self._handlers.get(message_type)
            logger.info(
                "WS message received: type=%s request_id=%s handler_found=%s",
                message_type.value,
                request_id,
                handler is not None,
            )
            if handler is None:
                logger.warning("No handler for message type: %s", message_type)
                await self._send_error(
                    request_id,
                    ErrorCode.UNKNOWN_COMMAND,
                    f"Unknown command: {message_type}",
                )
                continue

            handled = await self._run_until_closed(
                self._dispatch(handler, message_type, request_id, data), closed
            )
            if not handled:
                logger.warning(
                    "Connection closed while handling %s (request_id=%s); "
                    "abandoning it",
                    message_type.value,
                    request_id,
                )
                return

    @staticmethod
    async def _run_until_closed(
        handling: Coroutine[Any, Any, None], closed: asyncio.Task[None]
    ) -> bool:
        """Run one handler, but stop waiting for it as soon as the socket closes.

        Handlers block on worker threads for up to minutes; waiting for them
        would delay noticing the drop, and so reconnecting, by just as long.
        Returns False when the socket closed first: the handler task is
        cancelled, its thread finishes in the background and its result is
        dropped (the server re-sends the action on the next connection).
        """
        task = asyncio.create_task(handling)
        try:
            await asyncio.wait({task, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.wait({task})
        if task.cancelled():
            return False
        task.result()
        return True

    async def _reject_undecodable_message(
        self, raw_message: str, error: ProtocolError
    ) -> None:
        """Answer a malformed/unknown message so the server does not wait for a timeout."""
        request_id = peek_request_id(raw_message)
        logger.error(
            "Failed to deserialize message (request_id=%s): %s", request_id, error
        )
        if request_id is None:
            return
        code = (
            ErrorCode.UNKNOWN_COMMAND
            if isinstance(error, UnknownMessageTypeError)
            else ErrorCode.INVALID_MESSAGE
        )
        await self._send_error(request_id, code, str(error))

    async def _dispatch(
        self,
        handler: MessageHandler,
        message_type: MessageType,
        request_id: str,
        data: dict[str, object],
    ) -> None:
        """Run a handler without letting its failure drop the connection.

        Agent actions run with the person step window hidden. A message the
        target cannot run is answered TARGET_MISMATCH without running it.
        """
        mismatch = _target_mismatch(message_type)
        if mismatch is not None:
            await self._send_error(request_id, ErrorCode.TARGET_MISMATCH, mismatch)
            return
        try:
            async with self._window_hidden(message_type):
                await handler(request_id, data)
        except ProtocolError as e:
            logger.error(
                "Invalid %s message (request_id=%s): %s", message_type, request_id, e
            )
            await self._send_error(request_id, ErrorCode.INVALID_MESSAGE, str(e))
        except Exception as e:
            logger.exception(
                "Handler for %s failed (request_id=%s)", message_type, request_id
            )
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    @contextlib.asynccontextmanager
    async def _window_hidden(self, message_type: MessageType) -> AsyncIterator[None]:
        """Hide the person step window for an agent action, if one could show.

        The window re-shows itself later (rules in person_window). A failure to hide
        never stops the action (it is logged). On a phone target the agent
        sees the phone's screen, so the window on the host stays up (D8).
        """
        window = self._window
        if (
            window is None
            or message_type in _WINDOW_SAFE_MESSAGE_TYPES
            or get_mobile_device() is not None
        ):
            yield
            return
        hold = window.hidden_for_action()
        try:
            await hold.__aenter__()
            entered = True
        except Exception:
            logger.warning(
                "Could not hide the person step window before %s",
                message_type.value,
                exc_info=True,
            )
            entered = False
        try:
            yield
        finally:
            if entered:
                try:
                    await hold.__aexit__(None, None, None)
                except Exception:
                    logger.warning(
                        "Person step window failed after %s",
                        message_type.value,
                        exc_info=True,
                    )

    async def _send_message(self, message: str) -> None:
        if self._connection is not None:
            await self._connection.send(message)

    async def _send_action_result(
        self, request_id: str, result: ActionResultPayload
    ) -> None:
        message = serialize_message(
            MessageType.ACTION_RESULT,
            request_id=request_id,
            success=result.success,
            message=result.message,
            duration_ms=result.duration_ms,
        )
        await self._send_message(message)

    async def _send_screenshot_response(
        self, request_id: str, result: ScreenshotResponsePayload
    ) -> None:
        message = serialize_message(
            MessageType.SCREENSHOT_RESPONSE,
            request_id=request_id,
            success=result.success,
            image_base64=result.image_base64,
            width=result.width,
            height=result.height,
            format=result.format,
        )
        await self._send_message(message)

    async def _send_error(
        self, request_id: str, code: ErrorCode, message: str, details: str = ""
    ) -> None:
        msg = serialize_message(
            MessageType.ERROR,
            request_id=request_id,
            code=code,
            message=message,
            details=details,
        )
        await self._send_message(msg)

    async def _handle_click(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_click_payload(data)
        try:
            result = await asyncio.to_thread(execute_click, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_double_click(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_double_click_payload(data)
        try:
            result = await asyncio.to_thread(execute_double_click, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_hover(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_hover_payload(data)
        try:
            result = await asyncio.to_thread(execute_hover, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_drag(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_drag_payload(data)
        try:
            result = await asyncio.to_thread(execute_drag, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_scroll(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_scroll_payload(data)
        try:
            result = await asyncio.to_thread(execute_scroll, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_type_text(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_type_text_payload(data)
        try:
            result = await asyncio.to_thread(execute_type_text, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_key_press(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_key_press_payload(data)
        try:
            result = await asyncio.to_thread(execute_key_press, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_screenshot(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = await asyncio.to_thread(execute_screenshot)
            await self._send_screenshot_response(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.SCREENSHOT_FAILED), str(e)
            )

    async def _handle_run_command(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_run_command_payload(data)
        loop = asyncio.get_running_loop()
        # Output belongs to the connection the command arrived on. After a
        # reconnect the abandoned thread may still be producing lines; they
        # must not leak onto the new connection.
        connection = self._connection

        def _on_output(line: str, stream: StreamName) -> None:
            if connection is None or self._connection is not connection:
                return
            future = asyncio.run_coroutine_threadsafe(
                self._send_command_output_line(connection, request_id, line, stream),
                loop,
            )
            try:
                future.result(timeout=5)
            except Exception:
                logger.warning(
                    "Failed to send command output line for request %s: %r",
                    request_id,
                    line,
                    exc_info=True,
                )

        try:
            result = await asyncio.to_thread(
                execute_command_streaming, payload, _on_output, self._process_tracker
            )
            await self._send_command_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    @staticmethod
    async def _send_command_output_line(
        connection: ClientConnection, request_id: str, line: str, stream: StreamName
    ) -> None:
        message = serialize_message(
            MessageType.COMMAND_OUTPUT,
            request_id=request_id,
            line=line,
            stream=stream,
        )
        await connection.send(message)

    async def _send_command_result(
        self, request_id: str, result: CommandResultPayload
    ) -> None:
        message = serialize_message(
            MessageType.COMMAND_RESULT,
            request_id=request_id,
            success=result.success,
            stdout=result.stdout,
            stderr=result.stderr,
            return_code=result.return_code,
            duration_ms=result.duration_ms,
        )
        await self._send_message(message)

    async def _handle_ping(self, request_id: str, data: dict[str, object]) -> None:
        message = serialize_message(MessageType.PONG, request_id=request_id)
        await self._send_message(message)

    async def _handle_browser_navigate(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_navigate_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_navigate, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_click(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_click_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_click, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_double_click(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_click_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_double_click, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_type(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_type_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_type, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_hover(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_hover_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_hover, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_get_elements(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_target_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_get_elements, self._browser_session, payload
            )
            await self._send_browser_content_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_get_page_content(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_target_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_get_page_content, self._browser_session, payload
            )
            await self._send_browser_content_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_get_url(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_target_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_get_url, self._browser_session, payload
            )
            await self._send_browser_content_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_take_screenshot(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_target_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_take_screenshot, self._browser_session, payload
            )
            await self._send_screenshot_response(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.SCREENSHOT_FAILED), str(e)
            )

    async def _handle_browser_download(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_download_payload(data)
        try:
            result = await self._run_browser(
                execute_browser_download, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _browser_error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_browser_list_downloads(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = await self._run_browser(
                execute_browser_list_downloads, self._browser_session
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_start_interactive_cmd(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_start_interactive_cmd_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_start_interactive_cmd,
                self._session_manager,
                payload,
                self._interactive_cmd_timeout,
                self._process_tracker,
            )
            await self._send_interactive_output(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_send_input(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_send_input_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_send_input, self._session_manager, payload
            )
            await self._send_interactive_output(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_wait_for_command(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_wait_for_command_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_wait_for_command, self._session_manager, payload
            )
            await self._send_interactive_output(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_terminate_interactive_cmd(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_terminate_interactive_cmd_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_terminate_interactive_cmd, self._session_manager, payload
            )
            await self._send_interactive_output(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_list_interactive_sessions(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        sessions = await asyncio.to_thread(self._session_manager.list_sessions)
        await self._send_message(
            serialize_interactive_sessions_message(
                InteractiveSessionsPayload(sessions=sessions), request_id
            )
        )

    async def _handle_launch_app(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_launch_app_payload(data)
        try:
            if get_mobile_device() is not None:
                result = await asyncio.to_thread(execute_device_launch_app, payload)
            else:
                result = await asyncio.to_thread(
                    execute_launch_app, payload, self._process_tracker
                )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_close_app(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_close_app_payload(data)
        close = (
            execute_device_close_app
            if get_mobile_device() is not None
            else execute_close_app
        )
        try:
            result = await asyncio.to_thread(close, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_close_tab(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_close_tab_payload(data)
        try:
            result = await asyncio.to_thread(execute_close_tab, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_check_app_installed(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_check_app_installed_payload(data)
        check = (
            execute_device_check_app_installed
            if get_mobile_device() is not None
            else execute_check_app_installed
        )
        try:
            result = await asyncio.to_thread(check, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_scan_directory(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_scan_directory_payload(data)
        start = time.monotonic()
        result = await asyncio.to_thread(scan_directory, payload)
        await self._send_artifact_scan_result(
            request_id, scan_directory_result_to_json(result), start
        )

    async def _handle_inspect_paths(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_inspect_paths_payload(data)
        start = time.monotonic()
        result = await asyncio.to_thread(inspect_paths, payload)
        await self._send_artifact_scan_result(
            request_id, inspect_paths_result_to_json(result), start
        )

    async def _send_artifact_scan_result(
        self, request_id: str, result: object, start: float
    ) -> None:
        await self._send_message(
            serialize_message(
                MessageType.ARTIFACT_SCAN_RESULT,
                request_id=request_id,
                success=True,
                result=result,
                duration_ms=(time.monotonic() - start) * 1000,
            )
        )

    async def _handle_list_windows(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        start = time.monotonic()
        try:
            payload = parse_list_windows_payload(data)
        except ProtocolError as e:
            result = list_refusal(CaptureStatus.INVALID, str(e))
        else:
            try:
                result = await self._window_capture.list_windows(payload)
            except Exception:
                logger.exception("list_windows failed (request_id=%s)", request_id)
                result = list_refusal(CaptureStatus.CAPTURE_FAILED, "")
        await self._send_message(
            serialize_window_list_result_message(
                result, request_id, (time.monotonic() - start) * 1000
            )
        )

    async def _handle_capture_window(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        start = time.monotonic()
        try:
            payload = parse_capture_window_payload(data)
        except ProtocolError as e:
            result = capture_refusal(CaptureStatus.INVALID, str(e))
        else:
            try:
                result = await self._window_capture.capture(payload)
            except Exception:
                logger.exception("capture_window failed (request_id=%s)", request_id)
                result = capture_refusal(CaptureStatus.CAPTURE_FAILED, "")
        await self._send_message(
            serialize_window_capture_result_message(
                result, request_id, (time.monotonic() - start) * 1000
            )
        )

    async def _handle_list_device_apps(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        """The phone's installed apps (DEVICE_APPS_V1); always answers."""
        start = time.monotonic()
        try:
            result = await asyncio.to_thread(list_device_apps)
        except Exception:
            logger.exception("list_device_apps failed (request_id=%s)", request_id)
            result = device_apps_refusal(DeviceAppsStatus.FAILED, "")
        await self._send_message(
            serialize_device_apps_result_message(
                result, request_id, (time.monotonic() - start) * 1000
            )
        )

    async def _handle_cleanup_environment(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            # Cleanup closes the browser, so it runs on the Playwright thread.
            result = await self._run_browser(
                execute_cleanup,
                self._browser_session,
                self._session_manager,
                self._process_tracker,
                Path(self._config.cleanup_dir).expanduser(),
            )
            # Desktop browsers the web browser layer started (its profiles);
            # never raises.
            await self._web_hub().cleanup()
            if get_mobile_device() is not None:
                # After the host: the device's apps, keyboard and home screen.
                result = await asyncio.to_thread(execute_device_cleanup, result)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    # --- Web browser layer (WEB_BROWSER_V1) --------------------------------

    def _web_hub(self) -> WebBrowserHub:
        if self._web_browser_hub is None:
            mobile = self._config.mobile
            self._web_browser_hub = WebBrowserHub(
                WebBrowserContext(
                    target=current_target(),
                    host_os=platform.system(),
                    runner_root=RUNNING_PACKAGE_DIR,
                    driver_download=self._config.web_driver_download,
                    mobile=get_mobile_device(),
                    device_serial=(mobile.device_id or None) if mobile else None,
                )
            )
        return self._web_browser_hub

    async def _shutdown_web_browsers(self) -> None:
        """End the web browser layer's driver sessions (never raises)."""
        if self._web_browser_hub is not None:
            await self._web_browser_hub.shutdown()

    async def _handle_web_browser(
        self, message_type: MessageType, request_id: str, data: dict[str, object]
    ) -> None:
        """Every ``web_browser_*`` request: the hub answers within its budget.
        ProtocolError reaches ``_dispatch`` (INVALID_MESSAGE)."""
        try:
            result = await self._web_hub().handle(message_type, data)
        except ProtocolError:
            raise
        except ExecutionError as e:
            code, details = _web_browser_error(e)
            await self._send_error(request_id, code, str(e), details)
            return
        await self._send_message(
            serialize_web_browser_result_message(result, request_id)
        )

    # --- Phone and tablet target (MOBILE_DEVICE_V1) -----------------------

    async def _run_device_action(
        self, request_id: str, action: Callable[[], ActionResultPayload]
    ) -> None:
        try:
            result = await asyncio.to_thread(action)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(
                request_id, _error_code(e, ErrorCode.EXECUTION_FAILED), str(e)
            )

    async def _handle_swipe(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_swipe_payload(data)
        await self._run_device_action(
            request_id, functools.partial(execute_swipe, payload)
        )

    async def _handle_long_press(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_long_press_payload(data)
        await self._run_device_action(
            request_id, functools.partial(execute_long_press, payload)
        )

    async def _handle_press_button(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_press_button_payload(data)
        await self._run_device_action(
            request_id, functools.partial(execute_press_button, payload)
        )

    async def _handle_rotate_device(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_rotate_device_payload(data)
        await self._run_device_action(
            request_id, functools.partial(execute_rotate_device, payload)
        )

    async def _handle_hide_keyboard(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        await self._run_device_action(request_id, execute_hide_keyboard)

    async def _handle_open_url(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_open_url_payload(data)
        await self._run_device_action(
            request_id, functools.partial(execute_open_url, payload)
        )

    async def _handle_install_app(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_install_app_payload(data)
        await self._run_device_action(
            request_id, functools.partial(execute_install_app, payload)
        )

    async def send_device_status(self, status: DeviceStatusPayload) -> None:
        """Report the device's state (the device monitor's callback).

        Sent only on an accepted session; the latest is kept and re-sent
        right after every accepted handshake.
        """
        self._last_device_status = status
        await self._try_send_device_status()

    async def _try_send_device_status(self) -> None:
        status = self._last_device_status
        connection = self._connection
        # Before the handshake is accepted the server closes the socket on
        # any other message.
        if status is None or connection is None or not self._session_authenticated:
            return
        try:
            await connection.send(serialize_device_status(status))
        except ConnectionClosed:
            pass

    async def _send_interactive_output(
        self, request_id: str, result: InteractiveOutputPayload
    ) -> None:
        # ``output_dropped`` only when set, so a plain session's reply is
        # unchanged.
        extra: dict[str, object] = (
            {"output_dropped": True} if result.output_dropped else {}
        )
        message = serialize_message(
            MessageType.INTERACTIVE_OUTPUT,
            request_id=request_id,
            session_id=result.session_id,
            output=result.output,
            is_alive=result.is_alive,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            **extra,
        )
        await self._send_message(message)

    async def _send_browser_content_result(
        self, request_id: str, result: BrowserContentResultPayload
    ) -> None:
        message = serialize_message(
            MessageType.BROWSER_CONTENT_RESULT,
            request_id=request_id,
            success=result.success,
            content=result.content,
            duration_ms=result.duration_ms,
        )
        await self._send_message(message)

    # --- Self-update (SELF_UPDATE_V1) -------------------------------------
    #
    # The workflow lives in update_runner; the client only accepts or refuses
    # the message, sends the statuses (re-sending the last one after a
    # reconnect) and ends this runner once the new one took over.

    async def _handle_update_runner(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        update_id = peek_update_id(data)
        if update_id is None:
            # Nothing to report on: the server cannot match it to an update.
            await self._send_error(
                request_id,
                ErrorCode.INVALID_MESSAGE,
                "Missing or invalid 'update_id': expected a UUID",
            )
            return
        running = self._update_in_flight()
        if running is not None:
            if running.update_id == update_id:
                logger.warning("Ignoring a repeated update_runner for %s", update_id)
                return
            await self._refuse_update(
                update_id,
                f"Another update ({running.update_id}) is already in progress",
            )
            return
        if not await asyncio.to_thread(self._manual_spool.is_empty):
            # The update deletes this install's folder, spool included.
            spool = await asyncio.to_thread(self._manual_spool.describe)
            await self._refuse_update(
                update_id, _UPDATE_SPOOL_NOT_EMPTY.format(spool=spool)
            )
            return
        try:
            payload, layout = await asyncio.to_thread(prepare_update, data)
        except (ProtocolError, UpdateError, OSError) as e:
            await self._refuse_update(update_id, str(e) or type(e).__name__)
            return
        update = RunnerUpdate(
            payload,
            layout,
            self._config,
            self._send_update_status,
            before_device_release=self._shutdown_web_browsers,
        )
        self._update = update
        await self._send_update_status(update_id, UpdatePhase.ACCEPTED)
        # In the background, so pings and actions keep being answered.
        self._update_task = asyncio.create_task(self._run_update(update))

    def _update_in_flight(self) -> RunnerUpdate | None:
        task = self._update_task
        if task is None or task.done():
            return None
        return self._update

    def _update_id_to_keep(self) -> str | None:
        """The update the server must keep in flight across this handshake."""
        in_flight = self._update_in_flight()
        if in_flight is not None:
            return in_flight.update_id
        last = self._last_update_status
        update = self._update
        if (
            self._update_status_unsent
            and last is not None
            and update is not None
            and last[0] == update.update_id
        ):
            return update.update_id
        return None

    async def _run_update(self, update: RunnerUpdate) -> None:
        if await update.run():
            await self._finish_handover()

    async def _refuse_update(self, update_id: str, reason: str) -> None:
        """Report a refused ``update_runner`` on its update row.

        Sent once and not remembered, so it never replaces the status of an
        update that is still running (re-sent after a reconnect).
        """
        detail = reason[:UPDATE_STATUS_DETAIL_MAX_LENGTH]
        logger.error("Refused update %s: %s", update_id, detail)
        await self._try_send_update_status((update_id, UpdatePhase.FAILED, detail))

    async def _finish_handover(self) -> None:
        """The new runner took over: end this runner as if superseded."""
        self._handed_over = True
        self._log_handover()
        # The new runner shows its own window; on Windows this one's process
        # would also keep the old install's python.exe locked.
        await self._stop_person_window()
        self._running = False
        self._stop_event.set()
        if self._connection is not None:
            await self._connection.close()

    def _log_handover(self) -> None:
        handover = self._update.handover if self._update is not None else None
        if handover is not None:
            logger.info(
                "Updated to %s; the new Test Runner runs from %s",
                handover.version,
                handover.new_root,
            )

    def _log_handover_if_superseded(self, close_code: int) -> None:
        if (
            close_code == CloseCode.SUPERSEDED
            and self._update is not None
            and self._update.handover is not None
        ):
            self._handed_over = True
            self._log_handover()

    async def _handover_ends_session(self) -> bool:
        """After a drop while the new runner may be connecting, do not reconnect.

        Reconnecting would supersede the new runner. Wait for the update task
        instead: it ends on handover (stop) or on failure (reconnect to report
        it).
        """
        task = self._update_task
        update = self._update
        if update is None or update.handover is None or task is None or task.done():
            return self._handed_over
        logger.info("Waiting for the new Test Runner to take over before reconnecting")
        stopped = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
        return self._handed_over or not self._running

    async def _send_update_status(
        self, update_id: str, phase: UpdatePhase, detail: str = ""
    ) -> None:
        detail = detail[:UPDATE_STATUS_DETAIL_MAX_LENGTH]
        logger.info("Update %s: %s %s", update_id, phase.value, detail)
        self._last_update_status = (update_id, phase, detail)
        self._update_status_unsent = not await self._try_send_update_status(
            self._last_update_status
        )

    async def _try_send_update_status(
        self, status: tuple[str, UpdatePhase, str] | None
    ) -> bool:
        connection = self._connection
        # Before the handshake is accepted the server closes the socket on any
        # other message; _after_handshake re-sends what is still unsent.
        if status is None or connection is None or not self._session_authenticated:
            return False
        update_id, phase, detail = status
        message = serialize_message(
            MessageType.UPDATE_STATUS, update_id=update_id, phase=phase, detail=detail
        )
        try:
            await connection.send(message)
        except ConnectionClosed:
            return False
        return True

    async def _after_handshake(self) -> None:
        self._manual_uploader.attach(self._send_manual_upload)
        await self._try_send_device_status()
        if self._update_status_unsent or self._update_in_flight() is not None:
            self._update_status_unsent = not await self._try_send_update_status(
                self._last_update_status
            )
        update_id = self._config.update_id
        if update_id is not None and self._update_finish_task is None:
            self._update_finish_task = asyncio.create_task(
                finish_update_as_new_runner(
                    self._config, update_id, self._send_update_status
                )
            )

    async def _cancel_update_tasks(self) -> None:
        for task in (self._update_task, self._update_finish_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task})

    # --- Person steps (PERSON_STEPS_V1) -----------------------------------
    #
    # The window (person_window) shows the open Human in the loop steps the
    # server pushes and hands back the person's answers, which are sent one at
    # a time and matched to their result by request_id.

    async def _handle_person_step_state(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        # A push, not a request: a bad one is logged, never answered.
        try:
            state = parse_person_step_state_payload(data)
        except ProtocolError as e:
            logger.warning("Ignoring an invalid person_step_state: %s", e)
            return
        if self._window is None:
            logger.debug("Ignoring person_step_state: the window is off")
            return
        self._window.apply_state(state.steps, cases=state.cases)

    async def _handle_person_step_action_result(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        pending = self._pending_answer
        if pending is None or pending[0] != request_id:
            logger.info(
                "Ignoring a person step result no answer waits for (request_id=%s)",
                request_id,
            )
            return
        try:
            result = parse_person_step_action_result_payload(data)
        except ProtocolError as e:
            logger.warning("Invalid person_step_action_result: %s", e)
            result = _answer_failed(_ANSWER_UNREADABLE)
        self._settle_answer(request_id, result)

    async def _forward_answers(self, window: PersonWindow) -> None:
        async for action in window.actions():
            try:
                await self._send_answer(window, action)
            except Exception:
                logger.exception("Could not send the person's answer")
                window.show_result(_answer_failed(_ANSWER_NOT_CONNECTED))

    async def _send_answer(
        self, window: PersonWindow, action: PersonStepActionPayload
    ) -> None:
        # One answer at a time: the window disables its buttons meanwhile, so
        # this only bounds a misbehaving window process.
        if self._pending_answer is not None:
            window.show_result(_answer_failed(_ANSWER_BUSY))
            return
        connection = self._connection
        if connection is None or not self._session_authenticated:
            window.show_result(_answer_failed(_ANSWER_NOT_CONNECTED))
            return
        request_id = str(uuid.uuid4())
        expiry = asyncio.create_task(self._expire_answer(request_id))
        self._pending_answer = (request_id, expiry)
        logger.info(
            "Sending the person's answer (%s) for step %d round %d",
            action.action.value,
            action.person_step_id,
            action.round,
        )
        try:
            await connection.send(serialize_person_step_action(action, request_id))
        except ConnectionClosed:
            self._settle_answer(request_id, _answer_failed(_ANSWER_CONNECTION_LOST))

    async def _expire_answer(self, request_id: str) -> None:
        await asyncio.sleep(PERSON_STEP_ANSWER_TIMEOUT_SECONDS)
        logger.warning("No result for the person's answer (request_id=%s)", request_id)
        self._settle_answer(request_id, _answer_failed(_ANSWER_NO_REPLY))

    def _settle_answer(
        self, request_id: str, result: PersonStepActionResultPayload
    ) -> None:
        pending = self._pending_answer
        if pending is None or pending[0] != request_id:
            return
        self._pending_answer = None
        expiry = pending[1]
        if expiry is not asyncio.current_task():
            expiry.cancel()
        if self._window is not None:
            self._window.show_result(result)

    # Window v2: a pause (case_pause_action), a discussion message
    # (person_chat_send) or a help resolution (person_help_action), each
    # matched to its result by request_id; one of each kind at a time.

    async def _handle_case_pause_action_result(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        if not self._case_request_waits(CaseRequestKind.PAUSE, request_id):
            return
        try:
            result = parse_case_pause_action_result_payload(data)
        except ProtocolError as e:
            logger.warning("Invalid case_pause_action_result: %s", e)
            self._settle_case_request(
                CaseRequestKind.PAUSE, request_id, False, _ANSWER_UNREADABLE
            )
            return
        self._settle_case_request(
            CaseRequestKind.PAUSE, request_id, result.ok, result.message
        )

    async def _handle_person_case_action_result(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        # Shared by discussion messages and help resolutions.
        kind = next(
            (
                kind
                for kind in (CaseRequestKind.CHAT, CaseRequestKind.HELP)
                if self._case_request_waits(kind, request_id)
            ),
            None,
        )
        if kind is None:
            return
        try:
            result = parse_person_case_action_result_payload(data)
        except ProtocolError as e:
            logger.warning("Invalid person_case_action_result: %s", e)
            self._settle_case_request(kind, request_id, False, _ANSWER_UNREADABLE)
            return
        self._settle_case_request(kind, request_id, result.ok, result.message)

    def _case_request_waits(self, kind: CaseRequestKind, request_id: str) -> bool:
        pending = self._pending_case.get(kind)
        if pending is None or pending[0] != request_id:
            logger.info(
                "Ignoring a %s result nothing waits for (request_id=%s)",
                kind.value,
                request_id,
            )
            return False
        return True

    async def _forward_case_requests(self, window: PersonWindow) -> None:
        async for request in window.case_requests():
            kind = _case_request_kind(request)
            try:
                await self._send_case_request(window, kind, request)
            except Exception:
                logger.exception("Could not send the person's %s", kind.value)
                window.show_case_result(
                    kind,
                    False,
                    f"Not connected to Punk Hazard; your "
                    f"{_CASE_REQUEST_NOUNS[kind]} was not sent.",
                )

    async def _send_case_request(
        self, window: PersonWindow, kind: CaseRequestKind, request: PersonCaseRequest
    ) -> None:
        noun = _CASE_REQUEST_NOUNS[kind]
        if kind in self._pending_case:
            window.show_case_result(
                kind, False, f"Your previous {noun} is still being sent; wait for it."
            )
            return
        connection = self._connection
        if connection is None or not self._session_authenticated:
            window.show_case_result(
                kind,
                False,
                f"Not connected to Punk Hazard; send your {noun} again once the "
                "Test Runner reconnects.",
            )
            return
        request_id = str(uuid.uuid4())
        expiry = asyncio.create_task(self._expire_case_request(kind, request_id))
        self._pending_case[kind] = (request_id, expiry)
        logger.info(
            "Sending the person's %s for run case %d", kind.value, request.pivot_id
        )
        try:
            await connection.send(_serialize_case_request(request, request_id))
        except ConnectionClosed:
            self._settle_case_request(
                kind, request_id, False, self._case_connection_lost(kind)
            )

    async def _expire_case_request(
        self, kind: CaseRequestKind, request_id: str
    ) -> None:
        await asyncio.sleep(PERSON_CASE_REQUEST_TIMEOUT_SECONDS)
        logger.warning(
            "No result for the person's %s (request_id=%s)", kind.value, request_id
        )
        self._settle_case_request(
            kind,
            request_id,
            False,
            f"No answer from the server; check Punk Hazard whether your "
            f"{_CASE_REQUEST_NOUNS[kind]} arrived.",
        )

    @staticmethod
    def _case_connection_lost(kind: CaseRequestKind) -> str:
        return (
            "The connection to Punk Hazard was lost before it answered; check "
            f"Punk Hazard whether your {_CASE_REQUEST_NOUNS[kind]} arrived."
        )

    def _settle_case_request(
        self, kind: CaseRequestKind, request_id: str, ok: bool, message: str
    ) -> None:
        pending = self._pending_case.get(kind)
        if pending is None or pending[0] != request_id:
            return
        del self._pending_case[kind]
        expiry = pending[1]
        if expiry is not asyncio.current_task():
            expiry.cancel()
        if self._window is not None:
            self._window.show_case_result(kind, ok, message)

    # --- Manual run (MANUAL_RUN_V1) ----------------------------------------
    #
    # The window's Manual run tab: the server's snapshot and replies go to
    # the coordinator and the uploader; claim, release, pause and continue are
    # sent here (one at a time, matched by request_id), discussion messages in
    # their own slot; captures, notes and hand-ins stay on this machine
    # (manual_coordinator) until the uploader sends them, the marks so far go
    # out as progress reports (MANUAL_PAUSE_DISCUSS_V1).

    async def _recover_manual_spool(self) -> None:
        try:
            await asyncio.to_thread(self._manual_spool.recover)
            if not await asyncio.to_thread(self._manual_spool.is_empty):
                spool = await asyncio.to_thread(self._manual_spool.describe)
                logger.info("Manual run spool: %s; uploading once connected", spool)
        except OSError:
            logger.warning("Could not check the manual screenshot spool", exc_info=True)

    async def _stop_manual_upload(self) -> None:
        task = self._manual_upload_task
        self._manual_upload_task = None
        self._manual_uploader.detach()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    async def _send_manual_upload(self, message: str) -> None:
        # Also the coordinator's progress reports: any message on the socket.
        connection = self._connection
        if connection is None or not self._session_authenticated:
            raise ConnectionClosed(None, None)
        await connection.send(message)

    def _on_manual_upload_change(self) -> None:
        if self._manual is not None:
            self._manual.push_view()

    async def _handle_manual_run_state(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        # A push, not a request: a bad one is logged, never answered.
        try:
            state = parse_manual_run_state_payload(data)
        except ProtocolError as e:
            logger.warning("Ignoring an invalid manual_run_state: %s", e)
            return
        if self._manual is None:
            self._manual_uploader.on_server_pending(state.pending_uploads)
            return
        await self._manual.on_server_state(state)

    async def _handle_manual_case_submit_result(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = parse_manual_case_submit_result_payload(data)
        except ProtocolError as e:
            logger.warning("Invalid manual_case_submit_result: %s", e)
            self._manual_uploader.on_submit_result(request_id, None)
            return
        if not self._manual_uploader.on_submit_result(request_id, result):
            logger.info(
                "Ignoring a manual submit result nothing waits for (request_id=%s)",
                request_id,
            )

    async def _handle_manual_screenshot_ack(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            ack = parse_manual_screenshot_ack_payload(data)
        except ProtocolError as e:
            logger.warning("Invalid manual_screenshot_ack: %s", e)
            self._manual_uploader.on_ack(request_id, None)
            return
        if not self._manual_uploader.on_ack(request_id, ack):
            logger.info(
                "Ignoring a manual screenshot ack nothing waits for (request_id=%s)",
                request_id,
            )

    async def _forward_manual_requests(self, window: PersonWindow) -> None:
        # Captures run as their own tasks (they take a moment and serialize
        # themselves), so notes typed meanwhile are still saved.
        captures: set[asyncio.Task[None]] = set()
        async for request in window.manual_requests():
            if (
                isinstance(request, ManualCaseActionEvent)
                and request.action.action in _MANUAL_CLAIM_ACTIONS
            ):
                await self._send_manual_action(window, request.action)
                continue
            manual = self._manual
            if manual is None:
                continue
            if isinstance(request, ManualCaseActionEvent | ManualChatSendEvent):
                # A task too, created after the draft changes before it: the
                # progress flush waits for them (the coordinator's lock).
                task = asyncio.create_task(
                    self._send_manual_after_progress(window, manual, request)
                )
            else:
                task = asyncio.create_task(self._handle_manual_request(manual, request))
            captures.add(task)
            task.add_done_callback(captures.discard)

    @staticmethod
    async def _handle_manual_request(
        manual: ManualCoordinator, request: ManualWindowRequest
    ) -> None:
        try:
            await manual.handle(request)
        except Exception:
            logger.exception("A manual run request from the window failed")

    async def _send_manual_after_progress(
        self,
        window: PersonWindow,
        manual: ManualCoordinator,
        request: ManualCaseActionEvent | ManualChatSendEvent,
    ) -> None:
        """Pause, continue or a discussion message (MANUAL_PAUSE_DISCUSS_V1).

        A pause and a message first report the newest marks, so the server
        (and the agent) see them before it; continue needs no report.
        """
        kind = (
            ManualResultKind.CHAT
            if isinstance(request, ManualChatSendEvent)
            else _MANUAL_ACTION_KINDS[request.action.action]
        )
        try:
            if kind is not ManualResultKind.CONTINUE:
                await manual.flush_progress()
            if isinstance(request, ManualChatSendEvent):
                await self._send_manual_chat(window, manual, request.send)
            else:
                await self._send_manual_action(window, request.action)
        except Exception:
            logger.exception("Could not send the manual case %s", kind.value)
            window.show_manual_result(
                kind, False, "Not connected to Punk Hazard; nothing was sent."
            )

    async def _send_manual_chat(
        self,
        window: PersonWindow,
        manual: ManualCoordinator,
        send: ManualChatSendPayload,
    ) -> None:
        if self._pending_manual_chat is not None:
            window.show_manual_result(
                ManualResultKind.CHAT,
                False,
                "Your previous message is still being sent; wait for it.",
            )
            return
        if not manual.claimed(send.pivot_id, send.claim_token):
            window.show_manual_result(
                ManualResultKind.CHAT, False, _MANUAL_CHAT_NOT_YOURS
            )
            return
        connection = self._connection
        if connection is None or not self._session_authenticated:
            window.show_manual_result(
                ManualResultKind.CHAT,
                False,
                "Not connected to Punk Hazard; send your message again once the "
                "Test Runner reconnects.",
            )
            return
        request_id = str(uuid.uuid4())
        expiry = asyncio.create_task(self._expire_manual_chat(request_id))
        self._pending_manual_chat = (request_id, expiry)
        logger.info("Sending a discussion message about run case %d", send.pivot_id)
        try:
            await connection.send(serialize_manual_chat_send(send, request_id))
        except ConnectionClosed:
            self._settle_manual_chat(request_id, False, _MANUAL_CHAT_CONNECTION_LOST)

    async def _expire_manual_chat(self, request_id: str) -> None:
        await asyncio.sleep(MANUAL_CASE_ACTION_TIMEOUT_SECONDS)
        logger.warning(
            "No result for the manual discussion message (request_id=%s)", request_id
        )
        self._settle_manual_chat(
            request_id,
            False,
            "No answer from the server; check the discussion whether your "
            "message arrived.",
        )

    def _settle_manual_chat(self, request_id: str, ok: bool, message: str) -> None:
        pending = self._pending_manual_chat
        if pending is None or pending[0] != request_id:
            return
        self._pending_manual_chat = None
        expiry = pending[1]
        if expiry is not asyncio.current_task():
            expiry.cancel()
        if self._window is not None:
            self._window.show_manual_result(ManualResultKind.CHAT, ok, message)

    async def _send_manual_action(
        self, window: PersonWindow, action: ManualCaseActionPayload
    ) -> None:
        kind = _MANUAL_ACTION_KINDS[action.action]
        noun = _MANUAL_ACTION_NOUNS[action.action]
        if self._pending_manual_action is not None:
            window.show_manual_result(
                kind, False, f"Your previous {noun} is still being sent; wait for it."
            )
            return
        connection = self._connection
        if connection is None or not self._session_authenticated:
            window.show_manual_result(
                kind,
                False,
                f"Not connected to Punk Hazard; {noun} the case again once the "
                "Test Runner reconnects.",
            )
            return
        request_id = str(uuid.uuid4())
        expiry = asyncio.create_task(self._expire_manual_action(request_id, noun))
        self._pending_manual_action = (request_id, action, expiry)
        logger.info(
            "Sending manual case %s for run case %d",
            action.action.value,
            action.pivot_id,
        )
        try:
            await connection.send(serialize_manual_case_action(action, request_id))
        except ConnectionClosed:
            self._settle_manual_action(
                request_id, False, self._manual_connection_lost(action.action)
            )

    @staticmethod
    def _manual_connection_lost(action: ManualCaseActionKind) -> str:
        return (
            "The connection to Punk Hazard was lost before it answered; look at "
            f"the case list again before you {_MANUAL_ACTION_NOUNS[action]} it."
        )

    async def _expire_manual_action(self, request_id: str, noun: str) -> None:
        await asyncio.sleep(MANUAL_CASE_ACTION_TIMEOUT_SECONDS)
        logger.warning(
            "No result for the manual case %s (request_id=%s)", noun, request_id
        )
        self._settle_manual_action(
            request_id, False, "No answer from the server; check Punk Hazard."
        )

    async def _handle_manual_case_action_result(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        chat = self._pending_manual_chat
        if chat is not None and chat[0] == request_id:
            try:
                chat_result = parse_manual_case_action_result_payload(data)
            except ProtocolError as e:
                logger.warning("Invalid manual_case_action_result: %s", e)
                self._settle_manual_chat(request_id, False, _ANSWER_UNREADABLE)
                return
            self._settle_manual_chat(request_id, chat_result.ok, chat_result.message)
            return
        pending = self._pending_manual_action
        if pending is None or pending[0] != request_id:
            logger.info(
                "Ignoring a manual case action result nothing waits for "
                "(request_id=%s)",
                request_id,
            )
            return
        try:
            result = parse_manual_case_action_result_payload(data)
        except ProtocolError as e:
            logger.warning("Invalid manual_case_action_result: %s", e)
            self._settle_manual_action(request_id, False, _ANSWER_UNREADABLE)
            return
        action = pending[1]
        if (
            result.ok
            and action.action is ManualCaseActionKind.RELEASE
            and self._manual is not None
        ):
            await self._manual.on_released(action.claim_token)
        self._settle_manual_action(request_id, result.ok, result.message)

    def _settle_manual_action(self, request_id: str, ok: bool, message: str) -> None:
        pending = self._pending_manual_action
        if pending is None or pending[0] != request_id:
            return
        self._pending_manual_action = None
        _request_id, action, expiry = pending
        if expiry is not asyncio.current_task():
            expiry.cancel()
        if self._window is not None:
            self._window.show_manual_result(
                _MANUAL_ACTION_KINDS[action.action], ok, message
            )

    def _on_session_ended(self) -> None:
        """The socket is gone: the window shows it, waiting requests are released."""
        pending = self._pending_answer
        if pending is not None:
            self._settle_answer(pending[0], _answer_failed(_ANSWER_CONNECTION_LOST))
        pending_manual = self._pending_manual_action
        if pending_manual is not None:
            self._settle_manual_action(
                pending_manual[0],
                False,
                self._manual_connection_lost(pending_manual[1].action),
            )
        pending_chat = self._pending_manual_chat
        if pending_chat is not None:
            self._settle_manual_chat(
                pending_chat[0], False, _MANUAL_CHAT_CONNECTION_LOST
            )
        self._manual_uploader.detach()
        for kind, (request_id, _expiry) in list(self._pending_case.items()):
            self._settle_case_request(
                kind, request_id, False, self._case_connection_lost(kind)
            )
        if self._window is not None:
            self._window.set_connected(False)

    async def _stop_person_window(self) -> None:
        """Stop the window process for good (shutdown or self-update handover)."""
        window = self._window
        if window is None:
            return
        for task in (
            self._answer_task,
            self._case_request_task,
            self._manual_request_task,
        ):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task})
        pending = self._pending_answer
        if pending is not None:
            self._pending_answer = None
            pending[1].cancel()
        for _request_id, expiry in self._pending_case.values():
            expiry.cancel()
        self._pending_case.clear()
        for pending_slot in (
            self._pending_manual_action,
            self._pending_manual_chat,
        ):
            if pending_slot is not None:
                pending_slot[-1].cancel()
        self._pending_manual_action = None
        self._pending_manual_chat = None
        self._manual = None
        try:
            await window.stop()
        except Exception:
            logger.warning("Could not stop the person step window", exc_info=True)
        # After stop(): close_app keeps protecting it until it is gone.
        if self._window_pid_source is not None:
            unregister_helper_pid_source(self._window_pid_source)
            self._window_pid_source = None
        # Nothing may start it again (apply_state would).
        self._window = None
