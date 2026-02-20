import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeAlias

import websockets
from websockets.asyncio.client import ClientConnection

from controller_client.browser_executor import (
    BrowserSession,
    execute_browser_click,
    execute_browser_download,
    execute_browser_get_elements,
    execute_browser_get_page_content,
    execute_browser_get_url,
    execute_browser_hover,
    execute_browser_navigate,
    execute_browser_take_screenshot,
    execute_browser_type,
)
from controller_client.config import ClientConfig
from controller_client.exceptions import AuthenticationError, ExecutionError
from controller_client.executor import (
    execute_click,
    execute_command_streaming,
    execute_drag,
    execute_hover,
    execute_key_press,
    execute_screenshot,
    execute_send_input,
    execute_start_interactive_cmd,
    execute_terminate_interactive_cmd,
    execute_type_text,
)
from controller_client.interactive_session import InteractiveSessionManager
from controller_client.protocol import (
    ActionResultPayload,
    BrowserContentResultPayload,
    CommandResultPayload,
    ErrorCode,
    InteractiveOutputPayload,
    MessageType,
    ScreenshotResponsePayload,
    StreamName,
    deserialize_server_message,
    parse_browser_click_payload,
    parse_browser_download_payload,
    parse_browser_hover_payload,
    parse_browser_navigate_payload,
    parse_browser_type_payload,
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
from controller_client.system_info import gather_system_info

logger = logging.getLogger(__name__)

CLIENT_VERSION = "0.1.0"
_MB = 1024 * 1024
MAX_MESSAGE_SIZE = 10 * _MB

MessageHandler: TypeAlias = Callable[
    [str, dict[str, object]], Coroutine[Any, Any, None]
]


class ControllerClient:
    def __init__(
        self,
        config: ClientConfig,
        interactive_cmd_timeout: float = 300.0,
    ) -> None:
        self._config = config
        self._running = False
        self._connection: ClientConnection | None = None
        self._browser_session = BrowserSession()
        self._session_manager = InteractiveSessionManager()
        self._interactive_cmd_timeout = interactive_cmd_timeout
        self._handlers: dict[MessageType, MessageHandler] = {
            MessageType.HANDSHAKE_ACK: self._handle_handshake_ack,
            MessageType.CLICK: self._handle_click,
            MessageType.HOVER: self._handle_hover,
            MessageType.DRAG: self._handle_drag,
            MessageType.TYPE_TEXT: self._handle_type_text,
            MessageType.KEY_PRESS: self._handle_key_press,
            MessageType.SCREENSHOT_REQUEST: self._handle_screenshot,
            MessageType.RUN_COMMAND: self._handle_run_command,
            MessageType.PING: self._handle_ping,
            MessageType.BROWSER_NAVIGATE: self._handle_browser_navigate,
            MessageType.BROWSER_CLICK: self._handle_browser_click,
            MessageType.BROWSER_TYPE: self._handle_browser_type,
            MessageType.BROWSER_HOVER: self._handle_browser_hover,
            MessageType.BROWSER_GET_ELEMENTS: self._handle_browser_get_elements,
            MessageType.BROWSER_GET_PAGE_CONTENT: self._handle_browser_get_page_content,
            MessageType.BROWSER_GET_URL: self._handle_browser_get_url,
            MessageType.BROWSER_TAKE_SCREENSHOT: self._handle_browser_take_screenshot,
            MessageType.BROWSER_DOWNLOAD: self._handle_browser_download,
            MessageType.START_INTERACTIVE_CMD: self._handle_start_interactive_cmd,
            MessageType.SEND_INPUT: self._handle_send_input,
            MessageType.TERMINATE_INTERACTIVE_CMD: self._handle_terminate_interactive_cmd,
        }
        self._handshake_event = asyncio.Event()

    async def run(self) -> None:
        self._running = True
        attempt = 0

        while self._running and attempt < self._config.max_reconnect_attempts:
            try:
                await self._connect_and_listen()
                attempt = 0
            except AuthenticationError as e:
                logger.error("Authentication failed: %s", e)
                self._running = False
                break
            except Exception as e:
                attempt += 1
                logger.warning(
                    "Connection lost (%s). Attempt %d/%d. Reconnecting in %ds...",
                    e,
                    attempt,
                    self._config.max_reconnect_attempts,
                    self._config.reconnect_interval,
                )
                if self._running:
                    await asyncio.sleep(self._config.reconnect_interval)

        if attempt >= self._config.max_reconnect_attempts:
            logger.error("Max reconnect attempts reached. Shutting down.")
        self._running = False

    async def stop(self) -> None:
        self._running = False
        self._session_manager.terminate_all()
        await asyncio.to_thread(self._browser_session.close)
        if self._connection is not None:
            await self._connection.close()

    async def _connect_and_listen(self) -> None:
        url = self._config.ws_url
        logger.info("Connecting to %s", url)

        async with websockets.connect(url, max_size=MAX_MESSAGE_SIZE) as connection:
            self._connection = connection
            self._handshake_event.clear()
            await self._send_handshake()
            await self._message_loop(connection)

    async def _send_handshake(self) -> None:
        system_info = await asyncio.to_thread(gather_system_info)
        message = serialize_message(
            MessageType.HANDSHAKE,
            api_key=self._config.api_key,
            client_version=CLIENT_VERSION,
            system_info=system_info.to_dict(),
        )
        if self._connection is not None:
            await self._connection.send(message)
            logger.info("Handshake sent")

    async def _message_loop(self, connection: ClientConnection) -> None:
        async for raw_message in connection:
            if not self._running:
                break

            if not isinstance(raw_message, str):
                logger.warning("Received non-text message, ignoring")
                continue

            try:
                message_type, request_id, data = deserialize_server_message(raw_message)
            except Exception as e:
                logger.error("Failed to deserialize message: %s", e)
                continue

            handler = self._handlers.get(message_type)
            if handler is not None:
                await handler(request_id, data)
            else:
                logger.warning("No handler for message type: %s", message_type)
                await self._send_error(
                    request_id,
                    ErrorCode.UNKNOWN_COMMAND,
                    f"Unknown command: {message_type}",
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

    async def _handle_handshake_ack(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        ack = parse_handshake_ack_payload(data)
        if ack.status != "ok":
            raise AuthenticationError(f"Handshake rejected: {ack.status}")
        logger.info(
            "Connected to project '%s' (id=%s)", ack.project_name, ack.project_id
        )
        self._handshake_event.set()

    async def _handle_click(self, request_id: str, data: dict[str, object]) -> None:
        payload = parse_click_payload(data)
        try:
            result = await asyncio.to_thread(execute_click, payload)
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

        def _on_output(line: str, stream: StreamName) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._send_command_output_line(request_id, line, stream), loop
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
                execute_command_streaming, payload, _on_output
            )
            await self._send_command_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _send_command_output_line(
        self, request_id: str, line: str, stream: StreamName
    ) -> None:
        message = serialize_message(
            MessageType.COMMAND_OUTPUT,
            request_id=request_id,
            line=line,
            stream=stream,
        )
        await self._send_message(message)

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
            result = await asyncio.to_thread(
                execute_browser_navigate, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_browser_click(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_click_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_browser_click, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_browser_type(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_type_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_browser_type, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_browser_hover(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_hover_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_browser_hover, self._browser_session, payload
            )
            await self._send_action_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_browser_get_elements(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = await asyncio.to_thread(
                execute_browser_get_elements, self._browser_session
            )
            await self._send_browser_content_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_browser_get_page_content(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = await asyncio.to_thread(
                execute_browser_get_page_content, self._browser_session
            )
            await self._send_browser_content_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_browser_get_url(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = await asyncio.to_thread(
                execute_browser_get_url, self._browser_session
            )
            await self._send_browser_content_result(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.EXECUTION_FAILED, str(e))

    async def _handle_browser_take_screenshot(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = await asyncio.to_thread(
                execute_browser_take_screenshot, self._browser_session
            )
            await self._send_screenshot_response(request_id, result)
        except ExecutionError as e:
            await self._send_error(request_id, ErrorCode.SCREENSHOT_FAILED, str(e))

    async def _handle_browser_download(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        payload = parse_browser_download_payload(data)
        try:
            result = await asyncio.to_thread(
                execute_browser_download, self._browser_session, payload
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
