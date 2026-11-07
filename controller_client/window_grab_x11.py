"""X11 window and area grabs (any EWMH window manager on Xorg).

One ``Display`` per call (``x11_lib.open_display``):

* ``grab_rect``: ``XGetImage`` (ZPixmap) of the root window, which returns
  what is on screen there (with or without a compositing manager). The part
  of the rect outside the root window stays black, so image pixels keep
  matching the rect.
* ``is_unobscured``: ``window_capture_policy.unobscured_by_stacking`` over
  the inventory's stacking order and rects (managed windows and mapped
  override-redirect windows): no window of another process above it on the
  current desktop overlaps its rect.
* ``grab_window_only``: only while a compositing manager runs
  (``_NET_WM_CM_S<screen>`` has an owner), so the window is redirected and
  its off-screen pixmap holds its own current content:
  ``XCompositeNameWindowPixmap`` of its top-level (frame) window, then
  ``XGetImage``, cropped to the window's rect. Without a compositing manager
  or libXcomposite there is no faithful window-only image:
  ``window_obscured``.
* ``monitor_rect``: RandR 1.5 monitors (the whole root without libXrandr).

Under a shell that draws notifications and dialogs in its compositor stage
(``window_inventory_x11.shell_draws_overlays``: Cinnamon, GNOME Shell) the
root grab could show another program's banner the inventory never sees: no
window counts as unobscured there (window and region go through the
Composite window-only grab, ``window_obscured`` without it) and
``monitor_rect`` / ``grab_rect`` refuse with ``screen_not_clean``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from PIL import Image

from controller_client.capture_platform import WindowGrabBackend
from controller_client.protocol import CaptureStatus
from controller_client.window_capture_policy import unobscured_by_stacking
from controller_client.window_inventory_x11 import shell_draws_overlays
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopWindow,
    Rect,
)
from controller_client.x11_lib import X11Display, open_display

logger = logging.getLogger(__name__)

_GRAB_FAILED_DETAIL: Final = "the X server returned no image for the area"
_OUTSIDE_DETAIL: Final = "the area is outside the screen"
_NO_COMPOSITOR_DETAIL: Final = (
    "the window is covered by another window and no compositing manager "
    "runs, so it cannot be captured on its own; bring it to the front"
)
_NO_COMPOSITE_LIB_DETAIL: Final = (
    "the window is covered by another window and the X Composite library "
    "(libXcomposite) is missing, so it cannot be captured on its own; bring "
    "it to the front"
)
_NOT_DRAWN_DETAIL: Final = (
    "the window is covered and not drawn, so it cannot be captured on its own"
)
_SHELL_SCREEN_DETAIL: Final = "the desktop shell can draw notifications over the screen"
_SHELL_NO_COMPOSITOR_DETAIL: Final = (
    "the desktop shell can draw notifications over windows and no compositing "
    "manager runs, so the window cannot be captured on its own"
)
# Most a window's top-level (frame) may exceed its outer rect on either axis
# (decoration shadows) and still count as that window's own frame.
_FRAME_MARGIN: Final = 256
_WINDOW_GONE_DETAIL: Final = "the window closed during the capture"


def _window_xid(window: DesktopWindow) -> int:
    try:
        xid = int(window.window_id)
    except ValueError:
        xid = 0
    if xid <= 0:
        raise CaptureUnavailableError(
            CaptureStatus.WINDOW_NOT_FOUND, "the window id is not an X11 window"
        )
    return xid


def _refuse_shell_overlays(display: X11Display) -> None:
    """``screen_not_clean`` under a shell that draws overlays in its stage."""
    if shell_draws_overlays(display):
        raise CaptureUnavailableError(
            CaptureStatus.SCREEN_NOT_CLEAN, _SHELL_SCREEN_DETAIL
        )


def _grab_root_area(display: X11Display, rect: Rect) -> Image.Image:
    _refuse_shell_overlays(display)
    if rect.is_empty:
        raise CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _OUTSIDE_DETAIL)
    screen = display.root_rect()
    visible = rect.intersection(screen)
    if visible.is_empty:
        raise CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _OUTSIDE_DETAIL)
    image = display.get_image(
        display.root, visible.x, visible.y, visible.width, visible.height
    )
    if image is None:
        raise CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _GRAB_FAILED_DETAIL)
    if visible == rect:
        return image
    canvas = Image.new("RGB", (rect.width, rect.height))
    canvas.paste(image, (visible.x - rect.x, visible.y - rect.y))
    return canvas


class _X11Grab:
    """``WindowGrabBackend`` for X11."""

    def grab_rect(self, rect: Rect) -> Image.Image:
        with open_display() as display:
            return _grab_root_area(display, rect)

    def grab_window_only(self, window: DesktopWindow) -> Image.Image:
        xid = _window_xid(window)
        with open_display() as display:
            owner = display.selection_owner(f"_NET_WM_CM_S{display.screen}")
            if owner == 0:
                detail = (
                    _SHELL_NO_COMPOSITOR_DETAIL
                    if shell_draws_overlays(display)
                    else _NO_COMPOSITOR_DETAIL
                )
                raise CaptureUnavailableError(CaptureStatus.WINDOW_OBSCURED, detail)
            if not display.composite_available():
                raise CaptureUnavailableError(
                    CaptureStatus.WINDOW_OBSCURED, _NO_COMPOSITE_LIB_DETAIL
                )
            return self._composite_image(display, xid, window.rect)

    def _composite_image(
        self, display: X11Display, xid: int, rect: Rect
    ) -> Image.Image:
        client = display.attributes(xid)
        frame = display.top_level(xid)
        if client is None or frame is None:
            raise CaptureUnavailableError(
                CaptureStatus.WINDOW_NOT_FOUND, _WINDOW_GONE_DETAIL
            )
        frame_attrs = display.attributes(frame)
        if frame_attrs is None:
            raise CaptureUnavailableError(
                CaptureStatus.WINDOW_NOT_FOUND, _WINDOW_GONE_DETAIL
            )
        if not (client.viewable and frame_attrs.viewable):
            # An unmapped window has no current content (a stale pixmap at
            # best): never captured.
            raise CaptureUnavailableError(
                CaptureStatus.WINDOW_OBSCURED, _NOT_DRAWN_DETAIL
            )
        frame_width = frame_attrs.width + 2 * frame_attrs.border_width
        frame_height = frame_attrs.height + 2 * frame_attrs.border_width
        if (
            frame_width > rect.width + _FRAME_MARGIN
            or frame_height > rect.height + _FRAME_MARGIN
        ):
            # Not the window's own frame (a virtual root or a window
            # manager container holding other windows): its pixmap could
            # show other windows.
            logger.warning("X11 top-level window is much larger than the window")
            raise CaptureUnavailableError(
                CaptureStatus.WINDOW_OBSCURED, _NOT_DRAWN_DETAIL
            )
        pixmap = display.name_window_pixmap(frame)
        if pixmap is None:
            raise CaptureUnavailableError(
                CaptureStatus.WINDOW_OBSCURED, _NOT_DRAWN_DETAIL
            )
        try:
            # The pixmap covers the frame including its border.
            image = display.get_image(pixmap, 0, 0, frame_width, frame_height)
        finally:
            display.free_pixmap(pixmap)
        if image is None:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _GRAB_FAILED_DETAIL
            )
        # The frame's parent is the root: its x / y are root coordinates of
        # the outer border edge.
        origin_x, origin_y = frame_attrs.x, frame_attrs.y
        frame_rect = Rect(origin_x, origin_y, frame_width, frame_height)
        wanted = rect.intersection(frame_rect)
        if wanted.is_empty:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _GRAB_FAILED_DETAIL
            )
        left = wanted.x - origin_x
        top = wanted.y - origin_y
        return image.crop((left, top, left + wanted.width, top + wanted.height))

    def is_unobscured(
        self, window: DesktopWindow, windows: Sequence[DesktopWindow]
    ) -> bool:
        if not unobscured_by_stacking(window, windows):
            return False
        with open_display() as display:
            # The shell's stage overlays are in no inventory: never trust
            # the root grab there.
            return not shell_draws_overlays(display)

    def monitor_rect(self, window: DesktopWindow) -> Rect:
        with open_display() as display:
            # Only the screen target asks: refuse before any grab.
            _refuse_shell_overlays(display)
            monitors = display.monitors()
        best = max(monitors, key=lambda m: m.rect.intersection(window.rect).area)
        if best.rect.intersection(window.rect).area > 0:
            return best.rect
        for monitor in monitors:
            if monitor.primary:
                return monitor.rect
        return monitors[0].rect


GRAB: WindowGrabBackend = _X11Grab()
