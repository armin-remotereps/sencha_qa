"""WebSocket client that connects a vision worker to the server.

One worker parses one image at a time. Requests the server sends while the
GPU is busy wait in a small queue (VISION_MAX_QUEUE); beyond it the worker
answers BUSY so the server can try another worker right away.
"""

import asyncio
import json
import logging
import platform
import socket
import time
from dataclasses import dataclass
from typing import Final

import torch
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from vision_worker.config import WorkerConfig, default_worker_name
from vision_worker.engine import VisionEngine, detect_device, gpu_info
from vision_worker.exceptions import (
    AuthenticationError,
    ConnectionTerminatedError,
    EngineError,
    HandshakeError,
    ProtocolError,
)
from vision_worker.protocol import (
    HANDSHAKE_ACK_TIMEOUT_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    MAX_MESSAGE_BYTES,
    MAX_WORKER_NAME_LENGTH,
    TERMINAL_CLOSE_CODES,
    WORKER_CAPABILITIES,
    WORKER_VERSION,
    CloseCode,
    ErrorCode,
    HandshakeAckPayload,
    HandshakeStatus,
    MessageType,
    ParseRequestPayload,
    WorkerState,
    deserialize_message,
    parse_error_payload,
    parse_handshake_ack_payload,
    parse_parse_request_payload,
    serialize_message,
    serialize_parse_result,
)

logger = logging.getLogger(__name__)

OPEN_TIMEOUT_SECONDS: Final[float] = 15.0
CLOSE_TIMEOUT_SECONDS: Final[float] = 10.0
# Same keepalive tolerance as the Test Runner and Daphne (--ping-interval 30
# --ping-timeout 120): frames are ordered, so on a slow home uplink a pong can
# sit behind a multi-MiB parse result for a long time. The library's 20 s
# default would then close a healthy socket; 120 s still detects a half-open
# link within ~150 s.
PING_INTERVAL_SECONDS: Final[float] = 30.0
PING_TIMEOUT_SECONDS: Final[float] = 120.0
# Matches the server's VisionWorker.name column.
# Ack statuses that retrying cannot fix: a wrong token or an outdated worker.
_TERMINAL_HANDSHAKE_STATUSES: Final[frozenset[HandshakeStatus]] = frozenset(
    {HandshakeStatus.ERROR, HandshakeStatus.INCOMPATIBLE}
)
# A request dequeued sooner than this did not wait behind another one.
_QUEUED_THRESHOLD_SECONDS: Final[float] = 0.5
# Keeps the backoff exponent small; the delay is capped long before this.
_MAX_BACKOFF_EXPONENT: Final[int] = 16


@dataclass(frozen=True)
class _ParseJob:
    request_id: str
    payload: ParseRequestPayload
    received_at: float


def resolve_worker_name(config: WorkerConfig, device: str) -> str:
    name = config.worker_name or default_worker_name(device)
    return name[:MAX_WORKER_NAME_LENGTH]


def resolve_device_label(config: WorkerConfig) -> str:
    """The device to report before the model loads; never raises."""
    try:
        return detect_device(config.engine.device)
    except RuntimeError:
        return config.engine.device.value


def gather_system_info(device: str) -> dict[str, str | int]:
    info: dict[str, str | int] = {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "torch": torch.__version__,
        "device": device,
    }
    gpu = gpu_info(device)
    if gpu is not None:
        info["gpu"] = gpu.name
        info["vram_mb"] = gpu.vram_mb
    return info


def connect(url: str) -> websockets.connect:
    return websockets.connect(
        url,
        max_size=MAX_MESSAGE_BYTES,
        open_timeout=OPEN_TIMEOUT_SECONDS,
        close_timeout=CLOSE_TIMEOUT_SECONDS,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
    )


async def perform_handshake(
    connection: ClientConnection,
    *,
    token: str,
    worker_name: str,
    system_info: dict[str, str | int],
) -> HandshakeAckPayload:
    """Send the handshake and wait for its ack.

    An "error" or "incompatible" ack raises AuthenticationError (reconnecting
    will not help); a missing or malformed ack raises HandshakeError.
    """
    await connection.send(
        serialize_message(
            MessageType.HANDSHAKE,
            token=token,
            worker_name=worker_name,
            worker_version=WORKER_VERSION,
            capabilities=[capability.value for capability in WORKER_CAPABILITIES],
            system_info=system_info,
        )
    )
    logger.info(
        "Handshake sent (worker_name=%s, worker_version=%s)",
        worker_name,
        WORKER_VERSION,
    )
    try:
        raw_ack = await asyncio.wait_for(
            connection.recv(), timeout=HANDSHAKE_ACK_TIMEOUT_SECONDS
        )
    except TimeoutError as e:
        raise HandshakeError(
            f"No handshake ack within {HANDSHAKE_ACK_TIMEOUT_SECONDS}s"
        ) from e
    if not isinstance(raw_ack, str):
        raise HandshakeError("Handshake ack was not a text message")

    try:
        message_type, _, data = deserialize_message(raw_ack)
        if message_type is not MessageType.HANDSHAKE_ACK:
            raise ProtocolError(f"expected handshake_ack, got {message_type}")
        ack = parse_handshake_ack_payload(data)
    except ProtocolError as e:
        raise HandshakeError(f"Invalid handshake ack: {e}") from e

    if ack.status in _TERMINAL_HANDSHAKE_STATUSES:
        raise AuthenticationError(f"Handshake rejected ({ack.status}): {ack.message}")
    return ack


def _peek_request_id(raw_message: str) -> str | None:
    try:
        data = json.loads(raw_message)
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    request_id = data.get("request_id")
    return request_id if isinstance(request_id, str) else None


class _Session:
    """One authenticated connection: its parse queue and background tasks.

    Requests are tied to the socket they arrived on, since their replies can
    only go back over it; a reconnect starts with an empty queue.
    """

    def __init__(
        self, connection: ClientConnection, engine: VisionEngine, max_queue: int
    ) -> None:
        self._connection = connection
        self._engine = engine
        # The one running request plus the ones allowed to wait behind it.
        self._capacity = 1 + max_queue
        self._in_flight = 0
        # How long the last parse took; a queued request that cannot finish
        # within its timeout even at that pace is dropped unparsed.
        self._last_parse_seconds = 0.0
        self._queue: asyncio.Queue[_ParseJob] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._preload()),
            asyncio.create_task(self._heartbeat()),
            asyncio.create_task(self._consume()),
        ]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, ConnectionClosed):
                pass
            except Exception:
                logger.exception("Vision worker session task failed")
        self._tasks = []

    async def listen(self) -> None:
        async for raw_message in self._connection:
            if not isinstance(raw_message, str):
                logger.warning("Received non-text message, ignoring")
                continue
            await self._handle_message(raw_message)

    async def _handle_message(self, raw_message: str) -> None:
        try:
            message_type, request_id, data = deserialize_message(raw_message)
        except ProtocolError as e:
            request_id_or_none = _peek_request_id(raw_message)
            logger.error(
                "Invalid message from server (request_id=%s): %s",
                request_id_or_none,
                e,
            )
            if request_id_or_none is not None:
                await self._send_error(
                    request_id_or_none, ErrorCode.INVALID_MESSAGE, str(e)
                )
            return

        if message_type is MessageType.PARSE_REQUEST:
            await self._handle_parse_request(request_id, data)
        elif message_type is MessageType.ERROR:
            # Never answered, so an error cannot bounce back and forth.
            self._log_server_error(request_id, data)
        else:
            logger.warning(
                "Unexpected message type %s (request_id=%s)",
                message_type.value,
                request_id,
            )
            await self._send_error(
                request_id,
                ErrorCode.INVALID_MESSAGE,
                f"Unexpected message type: {message_type.value}",
            )

    @staticmethod
    def _log_server_error(request_id: str, data: dict[str, object]) -> None:
        try:
            error = parse_error_payload(data)
        except ProtocolError as e:
            logger.error(
                "Malformed error from server (request_id=%s): %s", request_id, e
            )
            return
        logger.error(
            "Server reported an error (request_id=%s): %s %s",
            request_id,
            error.code,
            error.message,
        )

    async def _handle_parse_request(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        received_at = time.monotonic()
        try:
            payload = parse_parse_request_payload(data)
        except ProtocolError as e:
            logger.error("Invalid parse_request (request_id=%s): %s", request_id, e)
            await self._send_error(request_id, ErrorCode.INVALID_MESSAGE, str(e))
            return

        readiness = self._engine.readiness()
        if readiness.state is not WorkerState.READY:
            await self._send_error(
                request_id,
                ErrorCode.NOT_READY,
                f"Vision worker is not ready ({readiness.state.value}): "
                f"{readiness.message}",
            )
            return
        if self._in_flight >= self._capacity:
            logger.info(
                "Answering BUSY (request_id=%s, in_flight=%d)",
                request_id,
                self._in_flight,
            )
            await self._send_error(
                request_id,
                ErrorCode.BUSY,
                f"Vision worker is busy ({self._in_flight} requests in flight)",
            )
            return

        self._in_flight += 1
        self._queue.put_nowait(_ParseJob(request_id, payload, received_at))
        logger.info(
            "Queued parse_request (request_id=%s, image_base64_len=%d, in_flight=%d)",
            request_id,
            len(payload.image_base64),
            self._in_flight,
        )

    async def _consume(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            finally:
                self._in_flight -= 1

    async def _process(self, job: _ParseJob) -> None:
        queue_seconds = time.monotonic() - job.received_at
        # Only a request that actually waited behind another is judged by the
        # last parse's pace; one taken straight away always runs, so a single
        # slow parse (cold CUDA, CPU device) cannot lock the worker out.
        waited = queue_seconds > _QUEUED_THRESHOLD_SECONDS
        expected_seconds = queue_seconds + (self._last_parse_seconds if waited else 0.0)
        if expected_seconds > job.payload.timeout_seconds:
            # The server stops waiting after timeout_seconds; a result it will
            # not read only wastes GPU time while it retries on another worker.
            logger.warning(
                "Dropping expired parse_request (request_id=%s, waited %.1fs + "
                "last parse %.1fs > %.1fs)",
                job.request_id,
                queue_seconds,
                self._last_parse_seconds,
                job.payload.timeout_seconds,
            )
            await self._send_error(
                job.request_id,
                ErrorCode.EXPIRED,
                f"Request waited {queue_seconds:.1f}s in the worker queue and "
                f"would not finish within its {job.payload.timeout_seconds:.1f}s "
                "timeout",
            )
            return

        parse_started = time.monotonic()
        try:
            result = await asyncio.to_thread(
                self._engine.parse, job.payload, queue_seconds=queue_seconds
            )
        except EngineError as e:
            logger.error(
                "Parse failed (request_id=%s, %s): %s", job.request_id, e.details(), e
            )
            await self._send_error(
                job.request_id, ErrorCode.PARSE_FAILED, str(e), details=e.details()
            )
            return
        except Exception as e:
            logger.exception("Unexpected parse failure (request_id=%s)", job.request_id)
            await self._send_error(job.request_id, ErrorCode.PARSE_FAILED, str(e))
            return
        self._last_parse_seconds = time.monotonic() - parse_started
        await self._connection.send(serialize_parse_result(job.request_id, result))
        logger.info(
            "Sent parse_result (request_id=%s, elements=%d)",
            job.request_id,
            len(result.elements),
        )

    async def _preload(self) -> None:
        """Load the models in a thread and report loading, then ready or failed.

        On a reconnect the models are usually loaded already, and only the
        current readiness is sent. A failed load is retried once per session.
        """
        if self._engine.readiness().state is not WorkerState.READY:
            await self._send_status()
            try:
                await asyncio.to_thread(self._engine.load)
            except EngineError:
                pass  # recorded in the readiness and logged by the engine
            except Exception:
                logger.exception("Unexpected OmniParser initialization failure")
        await self._send_status()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            await self._send_status()

    async def _send_status(self) -> None:
        status = self._engine.readiness().to_status_payload()
        await self._connection.send(
            serialize_message(
                MessageType.STATUS,
                state=status.state.value,
                message=status.message,
                device=status.device,
                phase=status.phase,
                load_seconds=status.load_seconds,
            )
        )

    async def _send_error(
        self, request_id: str, code: ErrorCode, message: str, details: str = ""
    ) -> None:
        await self._connection.send(
            serialize_message(
                MessageType.ERROR,
                request_id=request_id,
                code=code.value,
                message=message,
                details=details,
            )
        )


class VisionWorkerClient:
    def __init__(self, config: WorkerConfig, engine: VisionEngine) -> None:
        self._config = config
        self._engine = engine
        self._running = False
        self._connection: ClientConnection | None = None
        # Set by stop() so a pending reconnect backoff ends immediately.
        self._stop_event = asyncio.Event()
        # Whether the current connection got an "ok" ack; run() resets the
        # backoff only after such a session.
        self._session_authenticated = False
        self._worker_name = ""
        self._system_info: dict[str, str | int] = {}

    async def run(self) -> None:
        """Connect and serve until stopped; reconnect with backoff forever.

        Returns early when the server ends the session for good: a rejected
        token, an incompatible version, or a newer worker with the same name.
        """
        self._running = True
        self._stop_event.clear()
        device = await asyncio.to_thread(resolve_device_label, self._config)
        self._worker_name = resolve_worker_name(self._config, device)
        self._system_info = await asyncio.to_thread(gather_system_info, device)
        attempt = 0

        while self._running:
            self._session_authenticated = False
            try:
                await self._connect_and_serve()
                reason = "connection closed"
            except AuthenticationError as e:
                logger.error("%s. Not reconnecting.", e)
                return
            except ConnectionTerminatedError as e:
                logger.error("%s. Not reconnecting.", e)
                return
            except Exception as e:
                reason = str(e) or type(e).__name__

            if not self._running:
                break
            if self._session_authenticated:
                attempt = 0
            attempt += 1
            delay = self._reconnect_delay(attempt)
            logger.warning(
                "Connection lost (%s). Reconnect attempt %d in %ds...",
                reason,
                attempt,
                delay,
            )
            await self._sleep_unless_stopped(delay)

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

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._connection is not None:
            await self._connection.close()

    async def _connect_and_serve(self) -> None:
        logger.info(
            "Connecting to %s as %s", self._config.server_url, self._worker_name
        )
        session_error: ConnectionClosed | None = None
        async with connect(self._config.server_url) as connection:
            self._connection = connection
            session: _Session | None = None
            try:
                await perform_handshake(
                    connection,
                    token=self._config.token,
                    worker_name=self._worker_name,
                    system_info=self._system_info,
                )
                logger.info("Connected as vision worker %r", self._worker_name)
                self._session_authenticated = True
                session = _Session(connection, self._engine, self._config.max_queue)
                session.start()
                await session.listen()
            except ConnectionClosed as e:
                session_error = e
            finally:
                self._connection = None
                if session is not None:
                    await session.close()

        # Leaving the context manager closed the socket, so close_code is set.
        self._raise_if_terminated(connection.close_code)
        if session_error is not None:
            raise session_error

    @staticmethod
    def _raise_if_terminated(close_code: int | None) -> None:
        if close_code not in TERMINAL_CLOSE_CODES:
            return
        if close_code == CloseCode.SUPERSEDED:
            logger.error(
                "Another vision worker connected with the same name and replaced "
                "this one. Give each worker its own VISION_WORKER_NAME."
            )
        raise ConnectionTerminatedError(close_code)
