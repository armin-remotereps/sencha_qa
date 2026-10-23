"""Channel-layer events between the parse dispatcher and the worker consumer.

``parse_screenshot`` (Celery) sends ``vision.parse`` to the chosen worker's
consumer channel; the consumer answers on the request's reply channel with
``vision.parse_result`` or ``vision.error``. ``PendingParses`` is the
consumer-side map from request id to reply channel, failed in bulk when the
worker socket drops.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Final, TypedDict

from channels.layers import BaseChannelLayer

from vision_worker.protocol import ParseResultPayload

logger = logging.getLogger(__name__)

PARSE_EVENT_TYPE: Final[str] = "vision.parse"
PARSE_RESULT_EVENT_TYPE: Final[str] = "vision.parse_result"
ERROR_EVENT_TYPE: Final[str] = "vision.error"
SUPERSEDED_EVENT_TYPE: Final[str] = "vision_worker.superseded"

# Allowance for clock skew between the Celery and Daphne hosts.
_DEADLINE_MARGIN_SECONDS: Final[float] = 1.0


class VisionParseEvent(TypedDict):
    type: str
    request_id: str
    reply_channel: str
    image_base64: str
    box_threshold: float | None
    iou_threshold: float | None
    # Wall-clock (time.time()) moment the waiter gives up; replies after it are
    # dropped. Absolute, so time the event spent reaching the consumer counts.
    deadline_epoch: float


def remaining_seconds(deadline_epoch: float) -> float:
    """Seconds a waiter with this deadline will still accept a reply."""
    return deadline_epoch - _DEADLINE_MARGIN_SECONDS - time.time()


@dataclass(frozen=True)
class _Pending:
    reply_channel: str
    deadline: float


class PendingParses:
    def __init__(self, channel_layer: BaseChannelLayer) -> None:
        self._channel_layer = channel_layer
        self._pending: dict[str, _Pending] = {}

    def register(
        self, request_id: str, reply_channel: str, deadline_epoch: float
    ) -> None:
        deadline = deadline_epoch - _DEADLINE_MARGIN_SECONDS
        self._pending[request_id] = _Pending(reply_channel, deadline)

    def _take_live(self, request_id: str) -> str | None:
        """Pop the request's reply channel, or None when nobody is waiting.

        A reply after the waiter gave up must not be sent: the channel layer
        buffers it in whichever Celery process next reads any channel, and an
        abandoned reply channel is never read, so the multi-MiB result would
        stay in that process's memory for good.
        """
        pending = self._pending.pop(request_id, None)
        if pending is None:
            logger.warning("Vision worker reply for unknown request %s", request_id)
            return None
        if time.time() > pending.deadline:
            logger.warning(
                "Dropping late vision worker reply for request %s", request_id
            )
            return None
        return pending.reply_channel

    async def send_result(self, request_id: str, result: ParseResultPayload) -> bool:
        reply_channel = self._take_live(request_id)
        if reply_channel is None:
            return False
        await self._channel_layer.send(
            reply_channel,
            {
                "type": PARSE_RESULT_EVENT_TYPE,
                "request_id": request_id,
                "annotated_image_base64": result.annotated_image_base64,
                "elements": [dataclasses.asdict(el) for el in result.elements],
                "image_width": result.image_width,
                "image_height": result.image_height,
                "ocr_seconds": result.ocr_seconds,
                "som_seconds": result.som_seconds,
                "queue_seconds": result.queue_seconds,
            },
        )
        return True

    async def send_error(
        self, request_id: str, code: str, message: str, details: str = ""
    ) -> bool:
        reply_channel = self._take_live(request_id)
        if reply_channel is None:
            return False
        await self._send_error_event(reply_channel, request_id, code, message, details)
        return True

    async def fail_all(self, code: str, message: str) -> int:
        """Answer every pending request with an error and forget them all.

        Called when the worker socket goes away, so waiters move on to another
        worker at once instead of sitting out their attempt timeout.
        """
        pending = self._pending
        self._pending = {}
        now = time.time()
        for request_id, entry in pending.items():
            if now > entry.deadline:
                continue
            await self._send_error_event(
                entry.reply_channel, request_id, code, message, ""
            )
        return len(pending)

    async def _send_error_event(
        self,
        reply_channel: str,
        request_id: str,
        code: str,
        message: str,
        details: str,
    ) -> None:
        await self._channel_layer.send(
            reply_channel,
            {
                "type": ERROR_EVENT_TYPE,
                "request_id": request_id,
                "code": code,
                "message": message,
                "details": details,
            },
        )
