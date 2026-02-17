import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from controller_client.client import CLIENT_VERSION, ControllerClient
from controller_client.config import ClientConfig
from controller_client.exceptions import AuthenticationError
from controller_client.protocol import MessageType

_DEFAULT_CONFIG = ClientConfig(
    host="localhost",
    port=8000,
    api_key="test-key",
    reconnect_interval=1,
    max_reconnect_attempts=3,
    log_level="DEBUG",
)


def _make_config(**overrides: Any) -> ClientConfig:
    return replace(_DEFAULT_CONFIG, **overrides)


class TestControllerClientInit:
    def test_creates_with_config(self) -> None:
        config = _make_config()
        client = ControllerClient(config)
        assert client._config == config
        assert client._running is False

    def test_handler_dispatch_has_all_server_types(self) -> None:
        config = _make_config()
        client = ControllerClient(config)
        expected = {
            MessageType.HANDSHAKE_ACK,
            MessageType.CLICK,
            MessageType.HOVER,
            MessageType.DRAG,
            MessageType.TYPE_TEXT,
            MessageType.KEY_PRESS,
            MessageType.SCREENSHOT_REQUEST,
            MessageType.RUN_COMMAND,
            MessageType.PING,
            MessageType.BROWSER_NAVIGATE,
            MessageType.BROWSER_CLICK,
            MessageType.BROWSER_TYPE,
            MessageType.BROWSER_HOVER,
            MessageType.BROWSER_GET_ELEMENTS,
            MessageType.BROWSER_GET_PAGE_CONTENT,
            MessageType.BROWSER_GET_URL,
            MessageType.BROWSER_TAKE_SCREENSHOT,
        }
        assert set(client._handlers.keys()) == expected


class TestHandshakeAck:
    @pytest.mark.asyncio
    async def test_successful_handshake(self) -> None:
        config = _make_config()
        client = ControllerClient(config)
        data: dict[str, object] = {
            "status": "ok",
            "project_id": "p1",
            "project_name": "Test Project",
        }
        await client._handle_handshake_ack("r1", data)
        assert client._handshake_event.is_set()

    @pytest.mark.asyncio
    async def test_rejected_handshake(self) -> None:
        config = _make_config()
        client = ControllerClient(config)
        data: dict[str, object] = {
            "status": "rejected",
            "project_id": "",
            "project_name": "",
        }
        with pytest.raises(AuthenticationError, match="rejected"):
            await client._handle_handshake_ack("r1", data)


class TestPingPong:
    @pytest.mark.asyncio
    async def test_ping_sends_pong(self) -> None:
        config = _make_config()
        client = ControllerClient(config)
        mock_conn = AsyncMock()
        client._connection = mock_conn

        await client._handle_ping("req-123", {})

        mock_conn.send.assert_called_once()
        sent_raw: str = mock_conn.send.call_args[0][0]
        sent = json.loads(sent_raw)
        assert sent["type"] == "pong"
        assert sent["request_id"] == "req-123"


class TestClickHandler:
    @pytest.mark.asyncio
    @patch("controller_client.client.execute_click")
    async def test_click_dispatches(self, mock_exec: MagicMock) -> None:
        from controller_client.protocol import ActionResultPayload

        mock_exec.return_value = ActionResultPayload(
            success=True, message="clicked", duration_ms=5.0
        )
        config = _make_config()
        client = ControllerClient(config)
        mock_conn = AsyncMock()
        client._connection = mock_conn

        data: dict[str, object] = {"x": 100, "y": 200, "button": "left"}
        await client._handle_click("r1", data)

        mock_exec.assert_called_once()
        mock_conn.send.assert_called_once()

    @pytest.mark.asyncio
    @patch("controller_client.client.execute_click")
    async def test_click_error_sends_error_message(self, mock_exec: MagicMock) -> None:
        from controller_client.exceptions import ExecutionError

        mock_exec.side_effect = ExecutionError("boom")
        config = _make_config()
        client = ControllerClient(config)
        mock_conn = AsyncMock()
        client._connection = mock_conn

        data: dict[str, object] = {"x": 0, "y": 0, "button": "left"}
        await client._handle_click("r1", data)

        mock_conn.send.assert_called_once()
        sent = json.loads(mock_conn.send.call_args[0][0])
        assert sent["type"] == "error"
        assert sent["code"] == "EXECUTION_FAILED"


class TestRunCommandHandler:
    @pytest.mark.asyncio
    @patch("controller_client.client.execute_command")
    async def test_run_command_dispatches(self, mock_exec: MagicMock) -> None:
        from controller_client.protocol import CommandResultPayload

        mock_exec.return_value = CommandResultPayload(
            success=True,
            stdout="hello\n",
            stderr="",
            return_code=0,
            duration_ms=12.3,
        )
        config = _make_config()
        client = ControllerClient(config)
        mock_conn = AsyncMock()
        client._connection = mock_conn

        data: dict[str, object] = {"command": "echo hello", "timeout": 30.0}
        await client._handle_run_command("r1", data)

        mock_exec.assert_called_once()
        mock_conn.send.assert_called_once()
        sent = json.loads(mock_conn.send.call_args[0][0])
        assert sent["type"] == "command_result"
        assert sent["success"] is True
        assert sent["stdout"] == "hello\n"
        assert sent["return_code"] == 0

    @pytest.mark.asyncio
    @patch("controller_client.client.execute_command")
    async def test_run_command_error_sends_error_message(
        self, mock_exec: MagicMock
    ) -> None:
        from controller_client.exceptions import ExecutionError

        mock_exec.side_effect = ExecutionError("boom")
        config = _make_config()
        client = ControllerClient(config)
        mock_conn = AsyncMock()
        client._connection = mock_conn

        data: dict[str, object] = {"command": "bad", "timeout": 5.0}
        await client._handle_run_command("r1", data)

        mock_conn.send.assert_called_once()
        sent = json.loads(mock_conn.send.call_args[0][0])
        assert sent["type"] == "error"
        assert sent["code"] == "EXECUTION_FAILED"


class TestStopClient:
    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self) -> None:
        config = _make_config()
        client = ControllerClient(config)
        client._running = True
        mock_conn = AsyncMock()
        client._connection = mock_conn

        await client.stop()
        assert client._running is False
        mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_without_connection(self) -> None:
        config = _make_config()
        client = ControllerClient(config)
        client._running = True

        await client.stop()
        assert client._running is False
