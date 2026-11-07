"""Turn a chosen window or monitor into an image, "what a person sees" first.

In order:

1. An unobscured window: grab its rect on the screen (the app's own menus,
   dropdowns and tooltips included).
2. An obscured window: the platform's window-only image, where it has a
   faithful one (no other window in it, but no separate popups either);
   otherwise the platform raises ``window_obscured``.
3. Blank check: an all-black or uniform image is ``capture_blank`` and is
   never sent. A blank window-only image of a covered window names the
   covering processes (an always-on overlay such as a game or chat overlay
   is the usual cause), never their titles.

Blocking; runs on the capture worker thread.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from PIL import Image

from controller_client.capture_freshness import is_blank
from controller_client.capture_platform import WindowGrabBackend
from controller_client.protocol import CAPTURE_DETAIL_MAX_CHARS, CaptureStatus
from controller_client.window_capture_policy import (
    DenyRules,
    covering_processes,
    obscuring_windows,
)
from controller_client.window_model import CaptureUnavailableError, DesktopWindow, Rect

BLANK_DETAIL: Final[str] = (
    "the captured image is blank; the window may not be drawn yet, or the "
    "session is not shown on a display"
)

_COVERED_SUFFIX: Final[str] = "; close it or add it to the allowed applications"

Grab = Callable[[], Image.Image]


@dataclass(frozen=True)
class WindowGrabPlan:
    """How one window is grabbed, decided once from one inventory.

    ``grab``: repeatable. ``on_screen``: it grabs the window's visible rect
    from the screen (so a window that appears over it later would be in the
    shot); False for the window-only image. ``covered_by``: the processes
    whose windows cover it when ``on_screen`` is False.
    """

    grab: Grab
    on_screen: bool
    covered_by: tuple[str, ...]


def _rgb(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGB" else image.convert("RGB")


def window_is_unobscured(
    grab: WindowGrabBackend,
    window: DesktopWindow,
    windows: Sequence[DesktopWindow],
    allowed: frozenset[str],
    deny: DenyRules,
) -> bool:
    """The platform's obscured check over ``windows`` without allowlisted
    windows (``window_capture_policy.obscuring_windows``)."""
    return grab.is_unobscured(window, obscuring_windows(window, windows, allowed, deny))


def plan_window_grab(
    grab: WindowGrabBackend,
    window: DesktopWindow,
    windows: Sequence[DesktopWindow],
    allowed: frozenset[str],
    deny: DenyRules,
) -> WindowGrabPlan:
    """A repeatable grab of ``window``: its visible rect when it is
    unobscured on the current desktop, else its window-only image (decided
    once, from ``windows``, the inventory ``window`` came from)."""
    if window_is_unobscured(grab, window, windows, allowed, deny):
        rect = window.rect
        return WindowGrabPlan(
            grab=lambda: _rgb(grab.grab_rect(rect)), on_screen=True, covered_by=()
        )
    covering = covering_processes(
        window, obscuring_windows(window, windows, allowed, deny)
    )
    return WindowGrabPlan(
        grab=lambda: _rgb(grab.grab_window_only(window)),
        on_screen=False,
        covered_by=tuple(covering),
    )


def rect_grabber(grab: WindowGrabBackend, rect: Rect) -> Grab:
    """A repeatable grab of a desktop rect (the ``screen`` target's monitor)."""
    return lambda: _rgb(grab.grab_rect(rect))


def ensure_not_blank(image: Image.Image, covered_by: Sequence[str] = ()) -> None:
    """Raise ``capture_blank`` for an all-black or uniform image; when the
    image is a covered window's own, the detail names the processes that
    cover it (``covered_by``, process names only)."""
    if not is_blank(image):
        return
    if not covered_by:
        raise CaptureUnavailableError(CaptureStatus.CAPTURE_BLANK, BLANK_DETAIL)
    room = CAPTURE_DETAIL_MAX_CHARS - len("covered by ") - len(_COVERED_SUFFIX)
    names = ", ".join(covered_by)
    if len(names) > room:
        names = names[: max(room - 3, 0)].rstrip(", ") + "..."
    raise CaptureUnavailableError(
        CaptureStatus.CAPTURE_BLANK, f"covered by {names}{_COVERED_SUFFIX}"
    )
