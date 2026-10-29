import asyncio
import contextlib
import functools
import logging
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
from controller_client.exceptions import (
    AuthenticationError,
    BrowserElementNotFoundError,
    BrowserNotInstalledError,
    ConnectionTerminatedError,
    ExecutionError,
    HandshakeError,
    ProtocolError,
    UnknownMessageTypeError,
    UpdateError,
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
from controller_client.interactive_session import InteractiveSessionManager
from controller_client.person_window import PersonWindow
from controller_client.process_tracker import ProcessTracker
from controller_client.protocol import (
    CLIENT_VERSION,
    MAX_MESSAGE_SIZE,
    TERMINAL_CLOSE_CODES,
    UPDATE_STATUS_DETAIL_MAX_LENGTH,
    ActionResultPayload,
    BrowserContentResultPayload,
    CloseCode,
    CommandResultPayload,
    ErrorCode,
    InteractiveOutputPayload,
    MessageType,
    PersonStepActionPayload,
    PersonStepActionResultPayload,
    PersonStepQuestionAskPayload,
    PersonStepQuestionResultPayload,
    ScreenshotResponsePayload,
    StreamName,
    UpdatePhase,
    advertised_capabilities,
    deserialize_server_message,
    parse_browser_click_payload,
    parse_browser_download_payload,
    parse_browser_hover_payload,
    parse_browser_navigate_payload,
    parse_browser_target_payload,
    parse_browser_type_payload,
    parse_check_app_installed_payload,
    parse_click_payload,
    parse_close_app_payload,
    parse_close_tab_payload,
    parse_double_click_payload,
    parse_drag_payload,
    parse_handshake_ack_payload,
    parse_hover_payload,
    parse_key_press_payload,
    parse_launch_app_payload,
    parse_person_step_action_result_payload,
    parse_person_step_question_result_payload,
    parse_person_step_state_payload,
    parse_run_command_payload,
    parse_scroll_payload,
    parse_send_input_payload,
    parse_start_interactive_cmd_payload,
    parse_terminate_interactive_cmd_payload,
    parse_type_text_payload,
    parse_wait_for_command_payload,
    peek_request_id,
    peek_update_id,
    serialize_message,
    serialize_person_step_action,
    serialize_person_step_question,
)
from controller_client.system_info import gather_system_info
from controller_client.update_runner import (
    RunnerUpdate,
    finish_update_as_new_runner,
    prepare_update,
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
# The same for a question to the AI (only its acknowledgement; the answer
# comes later in a snapshot). Its own slot: questions and answers never wait
# for each other.
PERSON_STEP_QUESTION_TIMEOUT_SECONDS: Final[float] = 30.0
# Messages that never touch or capture the screen, so the person step window
# stays up while they are handled. Every other message is an agent action and
# runs with the window hidden (it must never be in a screenshot, the vision
# parse or under a click).
_WINDOW_SAFE_MESSAGE_TYPES: Final[frozenset[MessageType]] = frozenset(
    {
        MessageType.PING,
        MessageType.PERSON_STEP_STATE,
        MessageType.PERSON_STEP_ACTION_RESULT,
        MessageType.PERSON_STEP_QUESTION_RESULT,
        MessageType.UPDATE_RUNNER,
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
_QUESTION_NO_REPLY: Final[str] = (
    "No reply from the server; your question may not have been sent."
)
_QUESTION_CONNECTION_LOST: Final[str] = (
    "The connection to Punk Hazard was lost; check whether your question "
    "appears, else ask again."
)
_QUESTION_NOT_CONNECTED: Final[str] = (
    "Not connected to Punk Hazard; ask again once the Test Runner reconnects."
)
_QUESTION_BUSY: Final[str] = "Your previous question is still being sent; wait for it."
_QUESTION_UNAVAILABLE: Final[str] = (
    "Asking the AI is not available on this server; ask on Punk Hazard."
)

MessageHandler: TypeAlias = Callable[
    [str, dict[str, object]], Coroutine[Any, Any, None]
]

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _answer_failed(message: str) -> PersonStepActionResultPayload:
    return PersonStepActionResultPayload(ok=False, message=message, queued=False)


def _question_failed(message: str) -> PersonStepQuestionResultPayload:
    return PersonStepQuestionResultPayload(ok=False, message=message)


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
            MessageType.LAUNCH_APP: self._handle_launch_app,
            MessageType.CLOSE_APP: self._handle_close_app,
            MessageType.CLOSE_TAB: self._handle_close_tab,
            MessageType.CHECK_APP_INSTALLED: self._handle_check_app_installed,
            MessageType.CLEANUP_ENVIRONMENT: self._handle_cleanup_environment,
        }
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
        # Person steps (PERSON_STEPS_V1): the window, when enabled and Tk
        # works; None advertises no capability and the server sends nothing.
        self._window = person_window
        self._handlers[MessageType.PERSON_STEP_STATE] = self._handle_person_step_state
        self._handlers[MessageType.PERSON_STEP_ACTION_RESULT] = (
            self._handle_person_step_action_result
        )
        self._handlers[MessageType.PERSON_STEP_QUESTION_RESULT] = (
            self._handle_person_step_question_result
        )
        # Forwards the window's answers to the server (started by run()).
        self._answer_task: asyncio.Task[None] | None = None
        # The one answer waiting for its result: (request_id, its timeout).
        self._pending_answer: tuple[str, asyncio.Task[None]] | None = None
        # Ask AI (PERSON_STEP_ASSIST_V1): forwards the window's questions, but
        # only while this session's last snapshot said the server takes them.
        self._question_task: asyncio.Task[None] | None = None
        self._pending_question: tuple[str, asyncio.Task[None]] | None = None
        self._questions_enabled = False
        self._window_pid_source: Callable[[], int | None] | None = None
        if person_window is not None:
            self._window_pid_source = lambda: person_window.pid
            # close_app / close_tab never close the window process.
            register_helper_pid_source(self._window_pid_source)

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        attempt = 0
        window = self._window
        if window is not None and self._answer_task is None:
            self._answer_task = asyncio.create_task(self._forward_answers(window))
        if window is not None and self._question_task is None:
            self._question_task = asyncio.create_task(self._forward_questions(window))

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
        await self._cancel_update_tasks()
        await self._stop_person_window()
        self._process_tracker.kill_all()
        self._session_manager.terminate_all()
        # The executor is not shut down: stop() can run twice (signal, then
        # after run() returns) and the idle worker is joined at exit anyway.
        await self._run_browser(self._browser_session.close)
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
        ) as connection:
            self._connection = connection
            try:
                await self._perform_handshake(connection)
                if self._window is not None:
                    self._window.set_connected(True)
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

        logger.info(
            "Connected to project '%s' (id=%s)", ack.project_name, ack.project_id
        )
        self._session_authenticated = True

    async def _send_handshake(self, connection: ClientConnection) -> None:
        system_info = await asyncio.to_thread(gather_system_info)
        payload: dict[str, object] = {
            "api_key": self._config.api_key,
            "client_version": CLIENT_VERSION,
            "capabilities": [
                capability.value
                for capability in advertised_capabilities(
                    person_window=self._window is not None
                )
            ],
            "system_info": system_info.to_dict(),
        }
        if self._config.person_email is not None:
            # Names the member answering in the window; the server records
            # them only when the email is an active member of the project.
            payload["person_email"] = self._config.person_email
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

        Agent actions run with the person step window hidden.
        """
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
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    @contextlib.asynccontextmanager
    async def _window_hidden(self, message_type: MessageType) -> AsyncIterator[None]:
        """Hide the person step window for an agent action, if one could show.

        The window re-shows itself later (rules in person_window). A failure to hide
        never stops the action (it is logged).
        """
        window = self._window
        if window is None or message_type in _WINDOW_SAFE_MESSAGE_TYPES:
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
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_double_click(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_double_click_payload(data)
        try:
            result = await asyncio.to_thread(execute_double_click, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_hover(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_hover_payload(data)
        try:
            result = await asyncio.to_thread(execute_hover, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_drag(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_drag_payload(data)
        try:
            result = await asyncio.to_thread(execute_drag, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_scroll(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_scroll_payload(data)
        try:
            result = await asyncio.to_thread(execute_scroll, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_type_text(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_type_text_payload(data)
        try:
            result = await asyncio.to_thread(execute_type_text, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_key_press(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_key_press_payload(data)
        try:
            result = await asyncio.to_thread(execute_key_press, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_screenshot(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = await asyncio.to_thread(execute_screenshot)
            await self._send_screenshot_response(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.SCREENSHOT_FAILED, str(e))

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
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

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
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

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
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

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
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

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
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

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
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_launch_app(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_launch_app_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_launch_app, payload, self._process_tracker
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_close_app(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_close_app_payload(data)
        try:
            result = await asyncio.to_thread(execute_close_app, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_close_tab(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_close_tab_payload(data)
        try:
            result = await asyncio.to_thread(execute_close_tab, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_check_app_installed(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_check_app_installed_payload(data)
        try:
            result = await asyncio.to_thread(execute_check_app_installed, payload)
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

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
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _send_interactive_output(
        self, request_id: str, result: InteractiveOutputPayload
    ) -> None:
        message = serialize_message(
            MessageType.INTERACTIVE_OUTPUT,
            request_id=request_id,
            session_id=result.session_id,
            output=result.output,
            is_alive=result.is_alive,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
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
        try:
            payload, layout = await asyncio.to_thread(prepare_update, data)
        except (ProtocolError, UpdateError, OSError) as e:
            await self._refuse_update(update_id, str(e) or type(e).__name__)
            return
        update = RunnerUpdate(payload, layout, self._config, self._send_update_status)
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
        self._questions_enabled = state.questions_enabled
        self._window.apply_state(state.steps, questions_enabled=state.questions_enabled)

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

    async def _handle_person_step_question_result(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        pending = self._pending_question
        if pending is None or pending[0] != request_id:
            logger.info(
                "Ignoring a question result no question waits for (request_id=%s)",
                request_id,
            )
            return
        try:
            result = parse_person_step_question_result_payload(data)
        except ProtocolError as e:
            logger.warning("Invalid person_step_question_result: %s", e)
            result = _question_failed(_ANSWER_UNREADABLE)
        self._settle_question(request_id, result)

    async def _forward_questions(self, window: PersonWindow) -> None:
        async for question in window.questions():
            try:
                await self._send_question(window, question)
            except Exception:
                logger.exception("Could not send the person's question")
                window.show_question_result(_question_failed(_QUESTION_NOT_CONNECTED))

    async def _send_question(
        self, window: PersonWindow, question: PersonStepQuestionAskPayload
    ) -> None:
        # One question at a time (the window disables Ask meanwhile); an
        # answer waiting for its result never holds a question back.
        if not self._questions_enabled:
            window.show_question_result(_question_failed(_QUESTION_UNAVAILABLE))
            return
        if self._pending_question is not None:
            window.show_question_result(_question_failed(_QUESTION_BUSY))
            return
        connection = self._connection
        if connection is None or not self._session_authenticated:
            window.show_question_result(_question_failed(_QUESTION_NOT_CONNECTED))
            return
        request_id = str(uuid.uuid4())
        expiry = asyncio.create_task(self._expire_question(request_id))
        self._pending_question = (request_id, expiry)
        logger.info(
            "Sending the person's question for step %d round %d",
            question.person_step_id,
            question.round,
        )
        try:
            await connection.send(serialize_person_step_question(question, request_id))
        except ConnectionClosed:
            self._settle_question(
                request_id, _question_failed(_QUESTION_CONNECTION_LOST)
            )

    async def _expire_question(self, request_id: str) -> None:
        await asyncio.sleep(PERSON_STEP_QUESTION_TIMEOUT_SECONDS)
        logger.warning(
            "No result for the person's question (request_id=%s)", request_id
        )
        self._settle_question(request_id, _question_failed(_QUESTION_NO_REPLY))

    def _settle_question(
        self, request_id: str, result: PersonStepQuestionResultPayload
    ) -> None:
        pending = self._pending_question
        if pending is None or pending[0] != request_id:
            return
        self._pending_question = None
        expiry = pending[1]
        if expiry is not asyncio.current_task():
            expiry.cancel()
        if self._window is not None:
            self._window.show_question_result(result)

    def _on_session_ended(self) -> None:
        """The socket is gone: the window shows it, waiting requests are released."""
        pending = self._pending_answer
        if pending is not None:
            self._settle_answer(pending[0], _answer_failed(_ANSWER_CONNECTION_LOST))
        pending_question = self._pending_question
        if pending_question is not None:
            self._settle_question(
                pending_question[0], _question_failed(_QUESTION_CONNECTION_LOST)
            )
        # The next session's first snapshot says again whether it takes them.
        self._questions_enabled = False
        if self._window is not None:
            self._window.set_connected(False)

    async def _stop_person_window(self) -> None:
        """Stop the window process for good (shutdown or self-update handover)."""
        window = self._window
        if window is None:
            return
        for task in (self._answer_task, self._question_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task})
        pending = self._pending_answer
        if pending is not None:
            self._pending_answer = None
            pending[1].cancel()
        pending_question = self._pending_question
        if pending_question is not None:
            self._pending_question = None
            pending_question[1].cancel()
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
