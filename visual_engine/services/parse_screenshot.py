"""Send a screenshot to a vision worker and return the parsed elements.

Runs synchronously inside the Celery ``execution`` task. Workers are picked
round robin with a Redis counter shared by every Celery process; a busy worker
is skipped, and a lost, unready or silent worker is replaced by the next one
until the parse budget runs out. A parse is idempotent, so retrying is safe.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache

from agents.types import (
    AgentCancelledError,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
)
from projects.exceptions import VisualEngineError
from vision_worker.exceptions import ProtocolError
from vision_worker.protocol import (
    ErrorCode,
    ParseResultPayload,
    PixelElementPayload,
    parse_parse_result_payload,
)
from visual_engine.reply_routing import (
    ERROR_EVENT_TYPE,
    PARSE_EVENT_TYPE,
    PARSE_RESULT_EVENT_TYPE,
    VisionParseEvent,
)
from visual_engine.services.pool_status import ReadyWorker, list_ready_workers
from visual_engine.services.wait_for_ready_worker import poll_for_ready_worker
from visual_engine.types import VisualEngineState

logger = logging.getLogger(__name__)

_ROUND_ROBIN_KEY: Final[str] = "visual_engine:rr"
# Every busy pass re-sends the whole image to each busy worker, so back off
# exponentially instead of re-sending it every second.
_BUSY_RETRY_INITIAL_DELAY_SECONDS: Final[float] = 1.0
_BUSY_RETRY_MAX_DELAY_SECONDS: Final[float] = 8.0
_UNTRIED_WORKER_POLL_SECONDS: Final[float] = 2.0

# Deterministic failures: another worker would fail the same way.
_FATAL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {ErrorCode.PARSE_FAILED, ErrorCode.INVALID_MESSAGE}
)


class _Outcome(Enum):
    BUSY = "busy"
    # NOT_READY, WORKER_DISCONNECTED, EXPIRED or no reply in time: try the
    # next worker and do not come back to this one in this call.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _AttemptFailure:
    outcome: _Outcome
    reason: str


def parse_screenshot(
    image_base64: str,
    *,
    image_width: int,
    image_height: int,
    box_threshold: float | None = None,
    iou_threshold: float | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> PixelParseResult:
    """Parse ``image_base64`` on a ready vision worker.

    Raises ``VisualEngineError`` when no worker is ready within
    ``VISION_WORKER_WAIT_SECONDS``, when a worker reports a deterministic
    failure, when the result is not in the screenshot's coordinate space, or
    when no worker answered within ``VISION_PARSE_BUDGET_SECONDS``. Raises
    ``AgentCancelledError`` when the run is cancelled between attempts.
    """
    layer: Any = get_channel_layer()
    if layer is None:
        raise VisualEngineError("Channel layer is not configured")

    wait_timeout = float(settings.VISION_WORKER_WAIT_SECONDS)
    workers = list_ready_workers()
    if not workers:
        _wait_for_any_ready_worker(wait_timeout, cancellation_check)
        workers = list_ready_workers()

    deadline = time.monotonic() + float(settings.VISION_PARSE_BUDGET_SECONDS)
    attempt_timeout = float(settings.VISION_PARSE_ATTEMPT_TIMEOUT)
    start_index = _next_round_robin_index()
    # Keyed by channel, not row: a worker that dropped and reconnected under
    # the same name is a new connection worth trying again.
    excluded: set[str] = set()
    busy_delay = _BUSY_RETRY_INITIAL_DELAY_SECONDS
    last_error = "no vision worker is ready"

    while True:
        candidates = _rotate(workers, start_index, excluded)
        if not candidates:
            # Every live worker was tried; give a reconnecting one the rest of
            # the wait allowance before the case asks for help.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _budget_spent(last_error)
            workers = _wait_for_untried_worker(
                excluded, min(wait_timeout, remaining), cancellation_check
            )
            if not workers:
                raise _budget_spent(last_error)
            continue
        saw_busy = False
        for worker in candidates:
            _raise_if_cancelled(cancellation_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _budget_spent(last_error)
            outcome = _attempt(
                layer,
                worker,
                image_base64,
                box_threshold=box_threshold,
                iou_threshold=iou_threshold,
                timeout=min(attempt_timeout, remaining),
            )
            if isinstance(outcome, ParseResultPayload):
                return _build_parse_result(outcome, image_width, image_height)
            last_error = f"{worker.name}: {outcome.reason}"
            if outcome.outcome is _Outcome.BUSY:
                saw_busy = True
            else:
                excluded.add(worker.channel_name)
        if saw_busy:
            remaining = deadline - time.monotonic()
            if remaining <= busy_delay:
                raise _budget_spent(last_error)
            _raise_if_cancelled(cancellation_check)
            time.sleep(busy_delay)
            busy_delay = min(busy_delay * 2, _BUSY_RETRY_MAX_DELAY_SECONDS)
        workers = list_ready_workers()


def _wait_for_untried_worker(
    excluded: set[str],
    timeout: float,
    cancellation_check: Callable[[], bool] | None,
) -> list[ReadyWorker]:
    """Poll until a ready worker on a channel not yet tried shows up.

    Returns the ready list once one does, or ``[]`` when ``timeout`` runs out.
    Pool-level readiness is not enough here: excluded workers that still report
    ready (one that timed out, say) would end the wait at once.
    """
    deadline = time.monotonic() + timeout
    while True:
        _raise_if_cancelled(cancellation_check)
        workers = list_ready_workers()
        if any(worker.channel_name not in excluded for worker in workers):
            return workers
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        time.sleep(min(_UNTRIED_WORKER_POLL_SECONDS, remaining))


def _raise_if_cancelled(cancellation_check: Callable[[], bool] | None) -> None:
    if cancellation_check is not None and cancellation_check():
        raise AgentCancelledError("Test run cancelled while parsing the screen")


def _wait_for_any_ready_worker(
    timeout: float, cancellation_check: Callable[[], bool] | None
) -> None:
    logger.info("No vision worker is ready; waiting up to %.0fs for one", timeout)
    state = poll_for_ready_worker(
        timeout=timeout, cancellation_check=cancellation_check
    )
    if state is not VisualEngineState.READY:
        raise VisualEngineError(
            f"No vision worker is ready (waited {timeout:.0f}s, "
            f"state: {state.value})"
        )


def _next_round_robin_index() -> int:
    # add() seeds the shared counter once; incr() is atomic in Redis.
    cache.add(_ROUND_ROBIN_KEY, 0, timeout=None)
    try:
        return int(cache.incr(_ROUND_ROBIN_KEY))
    except ValueError:
        # The key was evicted between add() and incr(); start over.
        cache.add(_ROUND_ROBIN_KEY, 0, timeout=None)
        return 0


def _rotate(
    workers: list[ReadyWorker], start_index: int, excluded: set[str]
) -> list[ReadyWorker]:
    if not workers:
        return []
    offset = start_index % len(workers)
    rotated = workers[offset:] + workers[:offset]
    return [worker for worker in rotated if worker.channel_name not in excluded]


def _attempt(
    layer: Any,
    worker: ReadyWorker,
    image_base64: str,
    *,
    box_threshold: float | None,
    iou_threshold: float | None,
    timeout: float,
) -> ParseResultPayload | _AttemptFailure:
    request_id = str(uuid.uuid4())
    started = time.monotonic()
    try:
        reply = async_to_sync(_exchange)(
            layer,
            worker.channel_name,
            request_id,
            image_base64,
            box_threshold,
            iou_threshold,
            timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Vision worker %s did not answer parse %s within %.1fs",
            worker.name,
            request_id,
            timeout,
        )
        return _AttemptFailure(_Outcome.UNAVAILABLE, f"no reply within {timeout:.0f}s")
    round_trip = time.monotonic() - started

    reply_type = reply.get("type")
    if reply_type == PARSE_RESULT_EVENT_TYPE:
        try:
            result = parse_parse_result_payload(reply)
        except ProtocolError as exc:
            raise VisualEngineError(
                f"Vision worker {worker.name} returned a malformed result: {exc}"
            ) from exc
        logger.info(
            "Vision worker %s parsed %s: %d elements, round trip %.2fs "
            "(queue %.2fs, ocr %.2fs, som %.2fs)",
            worker.name,
            request_id,
            len(result.elements),
            round_trip,
            result.queue_seconds,
            result.ocr_seconds,
            result.som_seconds,
        )
        return result

    if reply_type != ERROR_EVENT_TYPE:
        raise VisualEngineError(
            f"Unexpected reply {reply_type!r} from vision worker {worker.name}"
        )
    return _classify_error(worker, request_id, reply, round_trip)


async def _exchange(
    layer: Any,
    channel_name: str,
    request_id: str,
    image_base64: str,
    box_threshold: float | None,
    iou_threshold: float | None,
    timeout: float,
) -> dict[str, Any]:
    # A fresh reply channel per attempt: a late reply to an abandoned attempt
    # lands on a channel nobody reads and expires.
    reply_channel: str = await layer.new_channel()
    event = VisionParseEvent(
        type=PARSE_EVENT_TYPE,
        request_id=request_id,
        reply_channel=reply_channel,
        image_base64=image_base64,
        box_threshold=box_threshold,
        iou_threshold=iou_threshold,
        deadline_epoch=time.time() + timeout,
    )
    await layer.send(channel_name, event)
    reply: dict[str, Any] = await asyncio.wait_for(
        layer.receive(reply_channel), timeout=timeout
    )
    return reply


def _classify_error(
    worker: ReadyWorker,
    request_id: str,
    reply: dict[str, Any],
    round_trip: float,
) -> _AttemptFailure:
    code = str(reply.get("code", ""))
    message = str(reply.get("message", ""))
    details = str(reply.get("details", ""))
    description = f"{code}: {message}" if message else code
    if details:
        description = f"{description} ({details})"

    if code in _FATAL_ERROR_CODES:
        raise VisualEngineError(f"Vision worker {worker.name} failed: {description}")

    logger.warning(
        "Vision worker %s answered parse %s with %s after %.2fs",
        worker.name,
        request_id,
        description,
        round_trip,
    )
    if code == ErrorCode.BUSY:
        return _AttemptFailure(_Outcome.BUSY, description)
    return _AttemptFailure(_Outcome.UNAVAILABLE, description)


def _budget_spent(last_error: str) -> VisualEngineError:
    return VisualEngineError(f"Visual engine did not return a result: {last_error}")


def _build_parse_result(
    result: ParseResultPayload, image_width: int, image_height: int
) -> PixelParseResult:
    # Elements must be in the screenshot's pixel space, or clicks land elsewhere.
    if (result.image_width, result.image_height) != (image_width, image_height):
        raise VisualEngineError(
            "Vision worker parsed an image of "
            f"{result.image_width}x{result.image_height}, "
            f"expected {image_width}x{image_height}"
        )
    # A center outside the screenshot would send a click off the captured
    # screen; drop such elements rather than trust them.
    elements = tuple(
        _build_pixel_element(el)
        for el in result.elements
        if 0 <= el.center_x < image_width and 0 <= el.center_y < image_height
    )
    return PixelParseResult(
        annotated_image=result.annotated_image_base64,
        elements=elements,
        image_width=result.image_width,
        image_height=result.image_height,
    )


def _build_pixel_element(element: PixelElementPayload) -> PixelUIElement:
    return PixelUIElement(
        index=element.index,
        type=element.type or "unknown",
        content=element.content,
        bbox=PixelBBox(
            x_min=element.bbox.x_min,
            y_min=element.bbox.y_min,
            x_max=element.bbox.x_max,
            y_max=element.bbox.y_max,
        ),
        center_x=element.center_x,
        center_y=element.center_y,
        interactivity=element.interactivity,
    )
