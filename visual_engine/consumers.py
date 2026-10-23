from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from vision_worker.exceptions import ProtocolError
from vision_worker.protocol import (
    HANDSHAKE_ACK_TIMEOUT_SECONDS,
    CloseCode,
    ErrorCode,
    HandshakePayload,
    HandshakeStatus,
    MessageType,
    deserialize_message,
    parse_error_payload,
    parse_handshake_payload,
    parse_parse_result_payload,
    parse_status_payload,
    serialize_message,
)
from visual_engine.exceptions import WorkerHandshakeError
from visual_engine.reply_routing import (
    PendingParses,
    VisionParseEvent,
    remaining_seconds,
)
from visual_engine.services.worker_auth import (
    check_worker_compatibility,
    is_valid_worker_token,
)
from visual_engine.services.worker_connection import claim_worker, release_worker
from visual_engine.services.worker_status import record_worker_status

logger = logging.getLogger(__name__)

# No valid handshake within HANDSHAKE_ACK_TIMEOUT_SECONDS of connecting. Not a
# terminal code: the worker reconnects.
_HANDSHAKE_TIMEOUT_CLOSE_CODE: Final[int] = 4003


class VisionWorkerConsumer(AsyncWebsocketConsumer):
    """One vision worker's WebSocket.

    The worker gets one handshake attempt per socket; afterwards it is
    addressed by this consumer's channel name (``vision.parse`` events) and
    its replies are routed back to the waiting dispatcher.
    """

    _worker_name: str
    _authenticated: bool
    _handshake_timer: asyncio.Task[None] | None
    _pending: PendingParses

    async def connect(self) -> None:
        self._worker_name = ""
        self._authenticated = False
        self._handshake_timer = None
        self._pending = PendingParses(self.channel_layer)
        await self.accept()
        self._handshake_timer = asyncio.create_task(self._close_without_handshake())

    async def disconnect(self, code: int) -> None:
        self._cancel_handshake_timer()
        if self._authenticated:
            # Release first, so a waiter retrying on WORKER_DISCONNECTED never
            # picks this channel again.
            await sync_to_async(release_worker)(
                name=self._worker_name, channel_name=self.channel_name
            )
        await self._pending.fail_all(
            ErrorCode.WORKER_DISCONNECTED, "Vision worker connection lost"
        )

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        if text_data is None:
            return

        try:
            message_type, request_id, data = deserialize_message(text_data)
        except ProtocolError as exc:
            await self._send_error(ErrorCode.INVALID_MESSAGE, str(exc))
            if not self._authenticated:
                await self.close()
            return

        if not self._authenticated:
            await self._handle_handshake(message_type, request_id, data)
            return

        if message_type is MessageType.STATUS:
            await self._handle_status(request_id, data)
        elif message_type is MessageType.PARSE_RESULT:
            await self._handle_parse_result(request_id, data)
        elif message_type is MessageType.ERROR:
            await self._handle_error(request_id, data)
        else:
            await self._send_error(
                ErrorCode.INVALID_MESSAGE,
                f"Unexpected message type: {message_type.value}",
                request_id,
            )

    async def vision_parse(self, event: VisionParseEvent) -> None:
        request_id = event["request_id"]
        # Time spent reaching this consumer counts against the waiter, so the
        # worker gets only what is left and a request already given up on is
        # never parsed.
        remaining = remaining_seconds(event["deadline_epoch"])
        if remaining <= 0:
            logger.warning(
                "Dropping parse %s: its waiter gave up before it reached the worker",
                request_id,
            )
            return
        self._pending.register(
            request_id, event["reply_channel"], event["deadline_epoch"]
        )
        await self.send(
            text_data=serialize_message(
                MessageType.PARSE_REQUEST,
                request_id=request_id,
                image_base64=event["image_base64"],
                box_threshold=event["box_threshold"],
                iou_threshold=event["iou_threshold"],
                timeout_seconds=remaining,
            )
        )

    async def vision_worker_superseded(self, event: dict[str, Any]) -> None:
        await self.close(code=CloseCode.SUPERSEDED)

    async def _handle_handshake(
        self, message_type: MessageType, request_id: str, data: dict[str, object]
    ) -> None:
        # One attempt per socket: any failure closes it.
        try:
            if message_type is not MessageType.HANDSHAKE:
                logger.warning(
                    "Vision worker handshake rejected: got %s before handshake",
                    message_type.value,
                )
                raise WorkerHandshakeError("Not authenticated. Send handshake first.")
            handshake = self._validate_handshake(data)
        except WorkerHandshakeError as exc:
            await self._reject(HandshakeStatus.ERROR, str(exc), request_id)
            return

        incompatibility = check_worker_compatibility(
            handshake.worker_version, handshake.capabilities
        )
        if incompatibility is not None:
            logger.warning(
                "Vision worker handshake rejected for %r (version %s): %s",
                handshake.worker_name,
                handshake.worker_version or "unknown",
                incompatibility,
            )
            await self._reject(
                HandshakeStatus.INCOMPATIBLE, incompatibility, request_id
            )
            return

        await self._establish(handshake, request_id)

    def _validate_handshake(self, data: dict[str, object]) -> HandshakePayload:
        try:
            handshake = parse_handshake_payload(data)
        except ProtocolError as exc:
            logger.warning("Vision worker handshake rejected: invalid handshake")
            raise WorkerHandshakeError(f"Invalid handshake: {exc}") from exc
        if not is_valid_worker_token(handshake.token):
            # Never log the token itself.
            logger.warning(
                "Vision worker handshake rejected for %r: invalid token",
                handshake.worker_name,
            )
            raise WorkerHandshakeError("Invalid token")
        return handshake

    async def _establish(self, handshake: HandshakePayload, request_id: str) -> None:
        self._cancel_handshake_timer()
        self._worker_name = handshake.worker_name
        previous_channel_name = await sync_to_async(claim_worker)(
            name=handshake.worker_name,
            channel_name=self.channel_name,
            worker_version=handshake.worker_version,
            capabilities=handshake.capabilities,
            system_info=handshake.system_info,
        )
        self._authenticated = True
        if previous_channel_name:
            await self.channel_layer.send(
                previous_channel_name, {"type": "vision_worker.superseded"}
            )
        logger.info(
            "Vision worker %r connected (version %s)",
            handshake.worker_name,
            handshake.worker_version,
        )
        await self.send(
            text_data=serialize_message(
                MessageType.HANDSHAKE_ACK,
                request_id=request_id,
                status=HandshakeStatus.OK,
                message="Connected",
            )
        )

    async def _reject(
        self, status: HandshakeStatus, message: str, request_id: str
    ) -> None:
        self._cancel_handshake_timer()
        await self.send(
            text_data=serialize_message(
                MessageType.HANDSHAKE_ACK,
                request_id=request_id,
                status=status,
                message=message,
            )
        )
        await self.close()

    async def _handle_status(self, request_id: str, data: dict[str, object]) -> None:
        try:
            status = parse_status_payload(data)
        except ProtocolError as exc:
            await self._send_error(ErrorCode.INVALID_MESSAGE, str(exc), request_id)
            return
        await sync_to_async(record_worker_status)(
            name=self._worker_name, channel_name=self.channel_name, status=status
        )

    async def _handle_parse_result(
        self, request_id: str, data: dict[str, object]
    ) -> None:
        try:
            result = parse_parse_result_payload(data)
        except ProtocolError as exc:
            logger.warning(
                "Vision worker %r sent a malformed parse_result: %s",
                self._worker_name,
                exc,
            )
            await self._pending.send_error(
                request_id,
                ErrorCode.INVALID_MESSAGE,
                f"Malformed parse_result: {exc}",
            )
            return
        await self._pending.send_result(request_id, result)

    async def _handle_error(self, request_id: str, data: dict[str, object]) -> None:
        try:
            error = parse_error_payload(data)
        except ProtocolError as exc:
            await self._pending.send_error(
                request_id,
                ErrorCode.INVALID_MESSAGE,
                f"Malformed error message: {exc}",
            )
            return
        await self._pending.send_error(
            request_id, error.code, error.message, error.details
        )

    async def _send_error(
        self, code: ErrorCode, message: str, request_id: str | None = None
    ) -> None:
        await self.send(
            text_data=serialize_message(
                MessageType.ERROR,
                request_id=request_id,
                code=code,
                message=message,
                details="",
            )
        )

    async def _close_without_handshake(self) -> None:
        await asyncio.sleep(HANDSHAKE_ACK_TIMEOUT_SECONDS)
        if not self._authenticated:
            logger.warning("Vision worker socket closed: no handshake in time")
            await self.close(code=_HANDSHAKE_TIMEOUT_CLOSE_CODE)

    def _cancel_handshake_timer(self) -> None:
        timer = self._handshake_timer
        self._handshake_timer = None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
