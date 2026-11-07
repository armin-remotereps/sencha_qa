"""Screenshots the tester takes in a Manual run (Pass, Fail, Retake, case).

Every capture runs hide → confirmed ack → settle → capture → show (plan D9):

1. ``PersonWindow.hidden_for_capture()`` sends the window a hide, waits for
   its ack and the compositor settle, and says whether the hide was
   confirmed. A hide that is not confirmed (no ack in time: the manager kills
   the window; a window that is not ready; an IPC error) aborts the capture:
   nothing is captured, so the window can never be in a tester screenshot.
2. After a confirmed hide, an extra settle (``EXTRA_SETTLE_SECONDS``: the
   window was just clicked and the hide animation may lag on macOS and
   Windows), then the whole screen is captured in a worker thread.
3. Leaving the hold shows the window again at once.
4. The image is downscaled to the protocol's ``MANUAL_SHOT_MAX_PIXELS`` when
   larger, then encoded (PNG; JPEG q85 when the PNG is over the protocol's
   ``MANUAL_SHOT_MAX_BYTES``, downscaled when still too large) with a small
   PNG thumbnail for the window. Storing it is the caller's job.

Captures are serialized by the process-wide screen-use lock
(``screen_use_lock``, shared with ``capture_window``), so a double click
yields one shot at a time; while an agent action holds the window, a capture waits for it
(holds nest).

On a phone target (plan D8) the shot is the phone's screen, so the window on
the host is never in it: no hide, ack or settle, the capture comes straight
from the target backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from PIL import Image

from controller_client.person_window import PersonWindow
from controller_client.protocol import (
    MANUAL_SHOT_MAX_BYTES,
    MANUAL_SHOT_MAX_PIXELS,
    ManualShotMime,
)
from controller_client.screen_capture import capture_screen
from controller_client.screen_use_lock import screen_use_lock
from controller_client.target import get_mobile_device

logger = logging.getLogger(__name__)

EXTRA_SETTLE_SECONDS: Final[float] = 0.25
HIDE_FAILED_MESSAGE: Final[str] = (
    "Could not hide the window, screenshot not taken. Try again."
)
_JPEG_QUALITY: Final[int] = 85
# Each downscale step when even the JPEG is too large, and the smallest scale.
_DOWNSCALE_STEP: Final[float] = 0.75
_MIN_SCALE: Final[float] = 0.1
_THUMBNAIL_BOX: Final[tuple[int, int]] = (360, 225)


class CaptureAbortedError(Exception):
    """No screenshot was taken; the message is shown in the window."""


class ShotTooLargeError(CaptureAbortedError):
    """Still over ``max_bytes`` after every downscale step (``too_large``)."""


@dataclass(frozen=True)
class EncodedShot:
    data: bytes
    mime: ManualShotMime
    # Lowercase hex of ``data``.
    sha256: str
    thumbnail_png: bytes
    # Pixels of the encoded image (after any downscale).
    width: int
    height: int


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue()


def _fit_pixels(image: Image.Image, max_pixels: int) -> Image.Image:
    pixels = image.width * image.height
    if pixels <= max_pixels:
        return image
    factor = math.sqrt(max_pixels / pixels)
    size = (
        max(1, math.floor(image.width * factor)),
        max(1, math.floor(image.height * factor)),
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def _encode(
    image: Image.Image, max_bytes: int
) -> tuple[bytes, ManualShotMime, tuple[int, int]]:
    """PNG, else JPEG q85, else JPEG downscaled step by step; the bytes, the
    type and the encoded size. Raises ShotTooLargeError."""
    data = _png(image)
    if len(data) <= max_bytes:
        return data, ManualShotMime.PNG, image.size
    rgb = image.convert("RGB")
    scale = 1.0
    size = rgb.size
    data = _jpeg(rgb)
    while len(data) > max_bytes:
        scale *= _DOWNSCALE_STEP
        if scale < _MIN_SCALE:
            raise ShotTooLargeError(
                "The screenshot is too large to upload; try a smaller screen."
            )
        size = (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale)))
        data = _jpeg(rgb.resize(size, Image.Resampling.LANCZOS))
    return data, ManualShotMime.JPEG, size


def encode_shot(
    image: Image.Image,
    *,
    max_bytes: int = MANUAL_SHOT_MAX_BYTES,
    max_pixels: int = MANUAL_SHOT_MAX_PIXELS,
) -> EncodedShot:
    """The screenshot as stored and uploaded, plus its thumbnail.

    A capture over ``max_pixels`` (a large multi-monitor desktop) is first
    downscaled to it, keeping the aspect ratio, so the server never refuses
    it; then PNG, JPEG q85 when the PNG is over ``max_bytes``, and smaller
    JPEGs while still too large. The limits default to the protocol's
    MANUAL_SHOT_MAX_BYTES / MANUAL_SHOT_MAX_PIXELS (a ``capture_window``
    request sends its own). Raises ShotTooLargeError (a CaptureAbortedError)
    when it cannot be made small enough.
    """
    image = _fit_pixels(image, max_pixels)
    data, mime, (width, height) = _encode(image, max_bytes)
    thumbnail = image.convert("RGB")
    thumbnail.thumbnail(_THUMBNAIL_BOX)
    return EncodedShot(
        data=data,
        mime=mime,
        sha256=hashlib.sha256(data).hexdigest(),
        thumbnail_png=_png(thumbnail),
        width=width,
        height=height,
    )


class ManualCapturer:
    """Takes tester screenshots with the window hidden (see the module docstring)."""

    def __init__(
        self,
        window: PersonWindow,
        grab: Callable[[], Image.Image] = capture_screen,
    ) -> None:
        self._window = window
        self._grab = grab

    async def capture(self) -> EncodedShot:
        """One screenshot; CaptureAbortedError when none was taken."""
        # Shared with WindowCaptureService: captures and desktop switches
        # never interleave.
        async with screen_use_lock():
            if get_mobile_device() is not None:
                image = await self._grab_image()
            else:
                async with self._window.hidden_for_capture() as confirmed:
                    if not confirmed:
                        logger.warning(
                            "Manual screenshot aborted: the window did not "
                            "confirm it was hidden"
                        )
                        raise CaptureAbortedError(HIDE_FAILED_MESSAGE)
                    await asyncio.sleep(EXTRA_SETTLE_SECONDS)
                    image = await self._grab_image()
            # Shown again already; encoding a large screen takes a moment.
            return await asyncio.to_thread(encode_shot, image)

    async def _grab_image(self) -> Image.Image:
        try:
            return await asyncio.to_thread(self._grab)
        except Exception as e:  # any backend failure aborts the capture
            logger.warning("Manual screenshot failed: %s", e, exc_info=True)
            raise CaptureAbortedError(f"The screen could not be captured: {e}") from e
