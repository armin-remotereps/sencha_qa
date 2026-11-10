"""Use case: a screenshot of the claimed case's test machine for the MCP
tester to look at (take_screenshot; docs/plan/feat-mcp-runner-tools.md, D4).

Analysis only: nothing is written (no database row, file or staging), and
there is no ``shot_id``, so it can never reach ``mark_step`` or ``hand_in``.

1. The gate (``open_machine_session``) has passed.
2. A process round-trip slot, the project's machine lock
   (MCP_SCREEN_LOCK_SECONDS), no reconnect wait and the call's deadline
   (MCP_SCREEN_DEADLINE_SECONDS, a cap on every reply wait), around the agent's own
   path ``controller_screenshot`` (the full screen of a desktop, ignoring
   the capture allowlist; the full device screen of a phone; the runner
   hides its person window first).
3. Outside the lock, one decode under a ``DECODE_SLOTS`` slot (waited for
   until the call's deadline, else ``[machine_busy]``) into a JPEG preview
   capped at MCP_SCREEN_PREVIEW_MAX_EDGE / MCP_SCREEN_PREVIEW_MAX_BYTES.

``width`` / ``height`` are the size the test machine reports for the shot:
screenshot pixels, the space ``click_at`` and the other ``*_at`` tools,
``scroll`` and ``swipe`` take
(the runner maps them to its input backend itself, iOS points included).
A test machine that reports 0 x 0 gets null and a note, as the agent does.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import time
from dataclasses import dataclass
from typing import Final

from PIL import Image, UnidentifiedImageError

from controller_client.protocol import MANUAL_SHOT_MAX_PIXELS
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.image_preview import make_preview, preview_size
from mcp_gateway.limits import (
    MCP_SCREEN_DEADLINE_SECONDS,
    MCP_SCREEN_LOCK_SECONDS,
    MCP_SCREEN_PREVIEW_MAX_BYTES,
    MCP_SCREEN_PREVIEW_MAX_EDGE,
)
from mcp_gateway.schemas import TakeScreenshotResponse
from mcp_gateway.services.machine_errors import (
    MachineErrorCode,
    machine_error,
    map_machine_exception,
    neutral_machine_text,
)
from mcp_gateway.services.machine_gate import MachineSession
from mcp_gateway.services.machine_round_trip import machine_lock, machine_round_trip
from mcp_gateway.services.stage_shot import DECODE_SLOTS
from mcp_gateway.untrusted_text import MACHINE_DATA_NOTE, fence_data
from projects.call_deadline import call_deadline
from projects.controller_reconnect_services import no_reconnect_wait
from projects.exceptions import ControllerActionError
from projects.services import controller_screenshot

logger = logging.getLogger(__name__)

# Per reply wait: controller_screenshot asks up to three times on a timeout,
# so all attempts fit within the call's deadline and the machine lock.
_REPLY_TIMEOUT_SECONDS: Final = MCP_SCREEN_DEADLINE_SECONDS / 3
_FORMATS: Final = ["PNG", "JPEG"]
_FAILED: Final = "The test machine could not take the screenshot."
_UNREADABLE: Final = (
    "The test machine sent a screenshot that cannot be read. Try again."
)
_TOO_LARGE: Final = (
    "The test machine's screen is too large to preview. Use a smaller screen "
    "resolution or capture_screenshot of one window."
)
_NOTE: Final = (
    "For looking only: this screenshot is never test evidence. The preview is "
    "{preview_width} x {preview_height}; the screen is {width} x {height} "
    "pixels, the coordinates click_at and the other *_at tools, scroll and "
    "swipe take. Map a preview "
    "point with x = preview_x * {width} / {preview_width} and "
    "y = preview_y * {height} / {preview_height}."
)
_NOTE_NO_SIZE: Final = (
    "For looking only: this screenshot is never test evidence. The test "
    "machine did not report its screen size, so preview points cannot be "
    "mapped to the coordinates of click_at and the other *_at tools, scroll "
    "or swipe; prefer the tools that "
    "take a description."
)


@dataclass(frozen=True)
class ScreenOutcome:
    response: TakeScreenshotResponse
    # Returned as image content; never stored.
    preview_jpeg: bytes


def take_screenshot(session: MachineSession) -> ScreenOutcome:
    """A preview of the test machine's screen. Raises ``McpToolError``."""
    started = time.monotonic()
    busy = machine_error(MachineErrorCode.MACHINE_BUSY)
    try:
        with (
            machine_round_trip(busy=busy),
            machine_lock(session.machine(), MCP_SCREEN_LOCK_SECONDS, busy=busy),
            no_reconnect_wait(),
            call_deadline(MCP_SCREEN_DEADLINE_SECONDS),
        ):
            shot = controller_screenshot(
                session.machine(), timeout=_REPLY_TIMEOUT_SECONDS
            )
    except McpToolError:
        raise
    except ControllerActionError as exc:
        # A refusal on the machine (no image, capture failed): its text can
        # carry runner text, so it is fenced as data, naming the test
        # machine only.
        mapped = map_machine_exception(exc)
        if mapped is not None:
            raise mapped from exc
        detail = fence_data(neutral_machine_text(str(exc)), MACHINE_DATA_NOTE)
        raise McpToolError(f"{_FAILED}\n{detail}" if detail else _FAILED) from exc
    except Exception as exc:
        mapped = map_machine_exception(exc)
        if mapped is None:
            raise
        raise mapped from exc
    jpeg = _preview(
        shot["image_base64"], deadline=started + MCP_SCREEN_DEADLINE_SECONDS
    )
    preview_width, preview_height = preview_size(jpeg)
    width, height = shot["width"], shot["height"]
    known_size = width > 0 and height > 0
    note = (
        _NOTE.format(
            width=width,
            height=height,
            preview_width=preview_width,
            preview_height=preview_height,
        )
        if known_size
        else _NOTE_NO_SIZE
    )
    return ScreenOutcome(
        response=TakeScreenshotResponse(
            width=width if known_size else None,
            height=height if known_size else None,
            preview_width=preview_width,
            preview_height=preview_height,
            note=note,
        ),
        preview_jpeg=jpeg,
    )


def _preview(image_base64: str, *, deadline: float) -> bytes:
    """The JPEG preview of the shot, from one decode under a decode slot."""
    try:
        data = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError):
        raise McpToolError(_UNREADABLE) from None
    if not DECODE_SLOTS.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise machine_error(MachineErrorCode.MACHINE_BUSY)
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as image:
            width, height = image.size
            # Checked on the header, before the bitmap is decoded.
            if width * height > MANUAL_SHOT_MAX_PIXELS:
                raise McpToolError(_TOO_LARGE)
            image.load()
            return make_preview(
                image,
                max_edge=MCP_SCREEN_PREVIEW_MAX_EDGE,
                max_bytes=MCP_SCREEN_PREVIEW_MAX_BYTES,
            )
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        logger.warning("take_screenshot: unreadable image from the test machine")
        raise McpToolError(_UNREADABLE) from None
    finally:
        DECODE_SLOTS.release()
