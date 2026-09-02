from __future__ import annotations

import json
from typing import cast

import pytest
from websockets.asyncio.client import ClientConnection

from controller_client.client import ControllerClient
from controller_client.config import ClientConfig
from controller_client.exceptions import AuthenticationError, OmniParserError
from controller_client.omniparser_executor import (
    OmniParserLoadResult,
    OmniParserReadiness,
)
from controller_client.protocol import CLIENT_CAPABILITIES, MessageType, OmniParserState

CLIENT = "controller_client.client"


class FakeConnection:
    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.sent: list[dict[str, object]] = []

    def __aiter__(self) -> FakeConnection:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, message: str) -> None:
        data: dict[str, object] = json.loads(message)
        self.sent.append(data)


class FakeSystemInfo:
    def to_dict(self) -> dict[str, str | int]:
        return {"os": "test", "cpu_count": 1}


def _client_with(connection: FakeConnection) -> ControllerClient:
    config = ClientConfig(
        host="localhost",
        port=8000,
        api_key="key",
        reconnect_interval=1,
        max_reconnect_attempts=1,
        log_level="INFO",
    )
    client = ControllerClient(config)
    client._connection = cast(ClientConnection, connection)
    client._running = True
    return client


def _message(message_type: str, request_id: str, **fields: object) -> str:
    return json.dumps({"type": message_type, "request_id": request_id, **fields})


@pytest.mark.asyncio
async def test_unknown_message_type_gets_error_reply_and_loop_continues() -> None:
    connection = FakeConnection([_message("teleport", "r1"), _message("ping", "r2")])
    client = _client_with(connection)

    await client._message_loop(cast(ClientConnection, connection))

    assert [m["type"] for m in connection.sent] == ["error", "pong"]
    assert connection.sent[0]["request_id"] == "r1"
    assert connection.sent[0]["code"] == "UNKNOWN_COMMAND"
    assert "teleport" in str(connection.sent[0]["message"])
    assert connection.sent[1]["request_id"] == "r2"


@pytest.mark.asyncio
async def test_malformed_message_gets_invalid_message_reply() -> None:
    connection = FakeConnection(
        ['{"request_id": "r1", "type": 42}', _message("ping", "r2")]
    )
    client = _client_with(connection)

    await client._message_loop(cast(ClientConnection, connection))

    assert connection.sent[0]["type"] == "error"
    assert connection.sent[0]["code"] == "INVALID_MESSAGE"
    assert connection.sent[0]["request_id"] == "r1"
    assert connection.sent[1]["type"] == "pong"


@pytest.mark.asyncio
async def test_handler_exception_gets_execution_failed_reply_and_loop_continues() -> (
    None
):
    connection = FakeConnection([_message("ping", "r1"), _message("ping", "r2")])
    client = _client_with(connection)
    original_ping = client._handlers[MessageType.PING]

    async def flaky_ping(request_id: str, data: dict[str, object]) -> None:
        if request_id == "r1":
            raise RuntimeError("pyautogui exploded")
        await original_ping(request_id, data)

    client._handlers[MessageType.PING] = flaky_ping

    await client._message_loop(cast(ClientConnection, connection))

    assert connection.sent[0]["type"] == "error"
    assert connection.sent[0]["code"] == "EXECUTION_FAILED"
    assert connection.sent[0]["request_id"] == "r1"
    assert connection.sent[0]["message"] == "pyautogui exploded"
    assert connection.sent[1] == {
        **connection.sent[1],
        "type": "pong",
        "request_id": "r2",
    }


@pytest.mark.asyncio
async def test_incompatible_handshake_ack_raises_with_server_message() -> None:
    connection = FakeConnection([])
    client = _client_with(connection)
    server_message = "Controller 0.1.0 is too old; download a new controller ZIP"

    with pytest.raises(AuthenticationError, match=server_message) as info:
        await client._handle_handshake_ack(
            "r1", {"status": "incompatible", "message": server_message}
        )

    assert "incompatible" in str(info.value)
    assert client._omniparser_task is None


@pytest.mark.asyncio
async def test_rejected_handshake_propagates_out_of_message_loop() -> None:
    connection = FakeConnection(
        [_message("handshake_ack", "r1", status="error", message="Invalid API key")]
    )
    client = _client_with(connection)

    with pytest.raises(AuthenticationError, match="Invalid API key"):
        await client._message_loop(cast(ClientConnection, connection))


@pytest.mark.asyncio
async def test_handshake_carries_version_and_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([])
    client = _client_with(connection)
    monkeypatch.setattr(f"{CLIENT}.gather_system_info", lambda: FakeSystemInfo())

    await client._send_handshake()

    handshake = connection.sent[0]
    assert handshake["type"] == "handshake"
    assert handshake["client_version"] == "0.2.0"
    assert handshake["capabilities"] == [c.value for c in CLIENT_CAPABILITIES]
    assert "find_element_local_v1" in cast(list[str], handshake["capabilities"])
    assert handshake["api_key"] == "key"


def _readiness(
    state: OmniParserState,
    message: str = "",
    device: str = "",
    weights_dir: str = "",
    phase: str = "",
    load_seconds: float = 0.0,
) -> OmniParserReadiness:
    return OmniParserReadiness(
        state=state,
        message=message,
        device=device,
        weights_dir=weights_dir,
        phase=phase,
        load_seconds=load_seconds,
    )


def _patch_readiness_sequence(
    monkeypatch: pytest.MonkeyPatch, snapshots: list[OmniParserReadiness]
) -> None:
    remaining = list(snapshots)
    monkeypatch.setattr(f"{CLIENT}.get_omniparser_readiness", lambda: remaining.pop(0))


@pytest.mark.asyncio
async def test_ok_ack_reports_omniparser_loading_then_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([])
    client = _client_with(connection)
    load_calls: list[str] = []

    def fake_load() -> OmniParserLoadResult:
        load_calls.append("load")
        return OmniParserLoadResult(device="cpu", weights_dir="/w", load_seconds=1.5)

    monkeypatch.setattr(f"{CLIENT}.load_omniparser_model", fake_load)
    _patch_readiness_sequence(
        monkeypatch,
        [
            _readiness(OmniParserState.LOADING, phase="not_started"),
            _readiness(
                OmniParserState.READY,
                "OmniParser ready on cpu",
                "cpu",
                "/w",
                "model_load",
                1.5,
            ),
        ],
    )

    await client._handle_handshake_ack(
        "r1", {"status": "ok", "project_id": "p", "project_name": "Proj"}
    )
    assert client._handshake_event.is_set()
    assert client._omniparser_task is not None
    await client._omniparser_task

    assert load_calls == ["load"]
    assert [m["type"] for m in connection.sent] == [
        "omniparser_status",
        "omniparser_status",
    ]
    assert connection.sent[0]["state"] == "loading"
    assert connection.sent[1]["state"] == "ready"
    assert connection.sent[1]["device"] == "cpu"
    assert connection.sent[1]["weights_dir"] == "/w"
    assert connection.sent[1]["load_seconds"] == 1.5
    assert "cpu" in str(connection.sent[1]["message"])


@pytest.mark.asyncio
async def test_ok_ack_reports_omniparser_failure_with_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([])
    client = _client_with(connection)

    def failing_load() -> OmniParserLoadResult:
        raise OmniParserError(
            "OmniParser weights not found at '/w'",
            phase="weights",
            device="",
            weights_dir="/w",
            code="OMNIPARSER_NOT_READY",
        )

    monkeypatch.setattr(f"{CLIENT}.load_omniparser_model", failing_load)
    _patch_readiness_sequence(
        monkeypatch,
        [
            _readiness(OmniParserState.LOADING, phase="not_started"),
            _readiness(
                OmniParserState.FAILED,
                "OmniParser weights not found at '/w'",
                "",
                "/w",
                "weights",
            ),
        ],
    )

    await client._handle_handshake_ack("r1", {"status": "ok"})
    assert client._omniparser_task is not None
    await client._omniparser_task

    assert connection.sent[0]["state"] == "loading"
    failed = connection.sent[1]
    assert failed["type"] == "omniparser_status"
    assert failed["state"] == "failed"
    assert failed["phase"] == "weights"
    assert failed["weights_dir"] == "/w"
    assert "weights not found" in str(failed["message"])


@pytest.mark.asyncio
async def test_ok_ack_still_reports_readiness_after_unexpected_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([])
    client = _client_with(connection)

    def exploding_load() -> OmniParserLoadResult:
        raise RuntimeError("segfault-ish")

    monkeypatch.setattr(f"{CLIENT}.load_omniparser_model", exploding_load)
    _patch_readiness_sequence(
        monkeypatch,
        [
            _readiness(OmniParserState.LOADING, phase="not_started"),
            _readiness(OmniParserState.FAILED, "segfault-ish", phase="model_load"),
        ],
    )

    await client._handle_handshake_ack("r1", {"status": "ok"})
    assert client._omniparser_task is not None
    await client._omniparser_task

    assert [m["state"] for m in connection.sent] == ["loading", "failed"]
    assert connection.sent[1]["message"] == "segfault-ish"


@pytest.mark.asyncio
async def test_ok_ack_on_warm_reconnect_only_reports_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([])
    client = _client_with(connection)

    def unexpected_load() -> OmniParserLoadResult:
        raise AssertionError("models are already loaded; load must not run")

    monkeypatch.setattr(f"{CLIENT}.load_omniparser_model", unexpected_load)
    ready = _readiness(
        OmniParserState.READY,
        "OmniParser ready on cuda",
        "cuda",
        "/w",
        "model_load",
        42.0,
    )
    monkeypatch.setattr(f"{CLIENT}.get_omniparser_readiness", lambda: ready)

    await client._handle_handshake_ack("r1", {"status": "ok"})
    assert client._omniparser_task is not None
    await client._omniparser_task

    assert [m["state"] for m in connection.sent] == ["ready"]
    assert connection.sent[0]["device"] == "cuda"


@pytest.mark.asyncio
async def test_find_element_omniparser_error_is_reported_with_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([])
    client = _client_with(connection)

    def not_ready(payload: object) -> object:
        raise OmniParserError(
            "OmniParser is not ready",
            phase="imports",
            device="",
            weights_dir="/w",
            code="OMNIPARSER_NOT_READY",
        )

    monkeypatch.setattr(f"{CLIENT}.execute_find_element", not_ready)

    await client._handle_find_element("r9", {})

    error = connection.sent[0]
    assert error["type"] == "error"
    assert error["request_id"] == "r9"
    assert error["code"] == "OMNIPARSER_NOT_READY"
    assert error["details"] == "phase=imports; device=; weights_dir=/w"


class _FakeConnect:
    """Stand-in for websockets.connect that records its keyword arguments."""

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.kwargs: dict[str, object] = {}

    def __call__(self, url: str, **kwargs: object) -> _FakeConnect:
        self.kwargs = kwargs
        return self

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest.mark.asyncio
async def test_connect_disables_client_keepalive_pings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([])
    fake_connect = _FakeConnect(connection)
    monkeypatch.setattr(f"{CLIENT}.websockets.connect", fake_connect)
    monkeypatch.setattr(f"{CLIENT}.gather_system_info", lambda: FakeSystemInfo())
    client = _client_with(connection)

    await client._connect_and_listen()

    assert "ping_interval" in fake_connect.kwargs
    assert fake_connect.kwargs["ping_interval"] is None
