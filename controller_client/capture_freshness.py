"""Pure Pillow checks on captured frames: freshness after a switch, blankness.

Used after a desktop switch and for window_grab's blank check. A browser resumes painting a window only once
it is shown, so the first grab after a desktop switch is never used alone:
``wait_for_stable_frame`` grabs again every FRESHNESS_INTERVAL_SECONDS until
two consecutive grabs match (at most FRESHNESS_MAX_CHANGED_FRACTION of the
pixels changed, which tolerates a blinking caret or a small spinner), within
FRESHNESS_BUDGET_SECONDS. No I/O besides the injected ``grab``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

from PIL import Image, ImageChops

# Grabs are compared every this many seconds.
FRESHNESS_INTERVAL_SECONDS: Final[float] = 0.25
# Longest wait, from the first grab, for two consecutive grabs to match.
FRESHNESS_BUDGET_SECONDS: Final[float] = 3.0
# Two grabs match when at most this fraction of their pixels changed.
FRESHNESS_MAX_CHANGED_FRACTION: Final[float] = 0.005
# A pixel counts as changed when one channel differs by more than this
# (absorbs dithering and compression noise).
_PIXEL_CHANGE_THRESHOLD: Final[int] = 16
# An image is blank when every channel stays at or below this (black with
# noise) or when it is one uniform colour.
_BLACK_MAX_LEVEL: Final[int] = 4
_CHANGED_LUT: Final[list[int]] = [
    0 if level <= _PIXEL_CHANGE_THRESHOLD else 255 for level in range(256)
]


def changed_fraction(first: Image.Image, second: Image.Image) -> float:
    """The fraction of pixels that differ between two frames (1.0 when their
    sizes differ)."""
    if first.size != second.size:
        return 1.0
    pixels = first.width * first.height
    if pixels == 0:
        return 0.0
    diff = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
    red, green, blue = diff.split()
    strongest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    changed = strongest.point(_CHANGED_LUT).histogram()[255]
    return changed / pixels


def frames_match(first: Image.Image, second: Image.Image) -> bool:
    """Whether two consecutive grabs show the same, settled content."""
    return changed_fraction(first, second) <= FRESHNESS_MAX_CHANGED_FRACTION


def is_blank(image: Image.Image) -> bool:
    """All black (with noise) or one uniform colour: never a real window."""
    extrema = image.convert("RGB").getextrema()
    bands: list[tuple[float, float]] = [
        (float(band[0]), float(band[1])) for band in extrema if isinstance(band, tuple)
    ]
    if not bands:
        return True
    uniform = all(low == high for low, high in bands)
    black = all(high <= _BLACK_MAX_LEVEL for _, high in bands)
    return uniform or black


def wait_for_stable_frame(
    grab: Callable[[], Image.Image],
    *,
    budget_seconds: float = FRESHNESS_BUDGET_SECONDS,
    interval_seconds: float = FRESHNESS_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Image.Image | None:
    """Grab until two consecutive grabs match; the last grab, or None when
    they kept changing for ``budget_seconds`` (``window_not_settled``).

    Blocking (worker thread only). Exceptions from ``grab`` propagate.
    """
    deadline = clock() + budget_seconds
    previous = grab()
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(interval_seconds, remaining))
        current = grab()
        if frames_match(previous, current):
            return current
        previous = current
