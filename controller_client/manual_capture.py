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

Captures are serialized by one lock, so a double click yields one shot at a
time; while an agent action holds the window, a capture waits for it
(holds nest).
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


@dataclass(frozen=True)
class EncodedShot:
    data: bytes
    mime: ManualShotMime
    # Lowercase hex of ``data``.
    sha256: str
    thumbnail_png: bytes


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue()


def encode_shot(image: Image.Image) -> EncodedShot:
    """The screenshot as stored and uploaded, plus its thumbnail.

    A capture over the protocol's MANUAL_SHOT_MAX_PIXELS (a large multi-
    monitor desktop) is first downscaled to it, keeping the aspect ratio, so
    the server never refuses it. Raises CaptureAbortedError when it cannot be
    made small enough.
    """
    pixels = image.width * image.height
    if pixels > MANUAL_SHOT_MAX_PIXELS:
        factor = math.sqrt(MANUAL_SHOT_MAX_PIXELS / pixels)
        size = (
            max(1, math.floor(image.width * factor)),
            max(1, math.floor(image.height * factor)),
        )
        image = image.resize(size, Image.Resampling.LANCZOS)
    data = _png(image)
    mime = ManualShotMime.PNG
    if len(data) > MANUAL_SHOT_MAX_BYTES:
        rgb = image.convert("RGB")
        scale = 1.0
        mime = ManualShotMime.JPEG
        data = _jpeg(rgb)
        while len(data) > MANUAL_SHOT_MAX_BYTES:
            scale *= _DOWNSCALE_STEP
            if scale < _MIN_SCALE:
                raise CaptureAbortedError(
                    "The screenshot is too large to upload; try a smaller screen."
                )
            size = (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale)))
            data = _jpeg(rgb.resize(size, Image.Resampling.LANCZOS))
    thumbnail = image.convert("RGB")
    thumbnail.thumbnail(_THUMBNAIL_BOX)
    return EncodedShot(
        data=data,
        mime=mime,
        sha256=hashlib.sha256(data).hexdigest(),
        thumbnail_png=_png(thumbnail),
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
        self._lock = asyncio.Lock()

    async def capture(self) -> EncodedShot:
        """One screenshot; CaptureAbortedError when none was taken."""
        async with self._lock:
            async with self._window.hidden_for_capture() as confirmed:
                if not confirmed:
                    logger.warning(
                        "Manual screenshot aborted: the window did not confirm "
                        "it was hidden"
                    )
                    raise CaptureAbortedError(HIDE_FAILED_MESSAGE)
                await asyncio.sleep(EXTRA_SETTLE_SECONDS)
                try:
                    image = await asyncio.to_thread(self._grab)
                except Exception as e:  # any backend failure aborts the capture
                    logger.warning("Manual screenshot failed: %s", e, exc_info=True)
                    raise CaptureAbortedError(
                        f"The screen could not be captured: {e}"
                    ) from e
            # Shown again already; encoding a large screen takes a moment.
            return await asyncio.to_thread(encode_shot, image)
