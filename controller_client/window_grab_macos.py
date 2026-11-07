"""Window and screen grabs for the window capture on macOS.

Exports ``GRAB``
(``capture_platform.WindowGrabBackend``).

* ``grab_rect``: ``screencapture -x -R x,y,w,h`` (global points, top-left
  origin; the image comes back in physical pixels).
* ``grab_window_only``: ``screencapture -x -o -l <CGWindowID>``, the window's
  own buffer without shadow, only for a window that is on screen
  (``CGWindowListCreateImage`` is obsoleted in the macOS 15 SDK, and a window
  on another Space or minimised can hold a stale frame).
* ``is_unobscured``: ``window_capture_policy.unobscured_by_stacking`` over
  the inventory, which holds every on-screen window of every layer (so
  floating panels, toasts and menus of other apps count; only the menu bar
  and the Dock's full-display window are left out), and the window lies on
  the displays.
* ``monitor_rect``: ``CGGetActiveDisplayList`` / ``CGDisplayBounds``.

``screencapture`` runs with a fixed argv (absolute path, no shell) and a
timeout, writing into a private temporary directory that is always removed.
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    import subprocess
    import tempfile
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Final

    import Quartz
    from PIL import Image

    from controller_client.capture_platform import WindowGrabBackend
    from controller_client.protocol import CaptureStatus
    from controller_client.screen_capture_macos import MACOS_PERMISSION_HINT
    from controller_client.window_capture_policy import unobscured_by_stacking
    from controller_client.window_inventory_macos import (
        cg_window_on_screen,
        display_rects,
    )
    from controller_client.window_model import (
        CaptureUnavailableError,
        DesktopWindow,
        Rect,
    )

    _SCREENCAPTURE: Final[str] = "/usr/sbin/screencapture"
    _SCREENCAPTURE_TIMEOUT_SECONDS: Final[float] = 15.0
    # Sample grid per axis for the on-the-displays check (fractions of the
    # rect).
    _SAMPLE_FRACTIONS: Final[tuple[float, ...]] = (0.02, 0.25, 0.5, 0.75, 0.98)

    _NOT_ON_SCREEN: Final[str] = (
        "the window is not on screen, so it has no current image to capture"
    )
    _TIMED_OUT: Final[str] = "the screen capture tool did not finish in time"
    _NOT_RUN: Final[str] = "the screen capture tool could not be started"
    _UNREADABLE: Final[str] = "the screen capture tool wrote an unreadable image"
    _NO_DISPLAY: Final[str] = "no display is active on the test machine"

    def _screencapture(arguments: Sequence[str]) -> Image.Image:
        """Run ``screencapture -x <arguments> -t png <file>`` and return the
        image in mode RGB. Alpha (window corners) is composited on white."""
        with tempfile.TemporaryDirectory(prefix="tr-capture-") as directory:
            target = Path(directory) / "shot.png"
            argv = [_SCREENCAPTURE, "-x", *arguments, "-t", "png", str(target)]
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=_SCREENCAPTURE_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CaptureUnavailableError(
                    CaptureStatus.CAPTURE_FAILED, _TIMED_OUT
                ) from exc
            except OSError as exc:
                raise CaptureUnavailableError(
                    CaptureStatus.CAPTURE_FAILED, _NOT_RUN
                ) from exc
            if (
                completed.returncode != 0
                or not target.exists()
                or target.stat().st_size == 0
            ):
                if not Quartz.CGPreflightScreenCaptureAccess():
                    raise CaptureUnavailableError(
                        CaptureStatus.PERMISSION_MISSING, MACOS_PERMISSION_HINT
                    )
                raise CaptureUnavailableError(
                    CaptureStatus.CAPTURE_FAILED,
                    "the screen capture tool failed "
                    f"(exit code {completed.returncode})",
                )
            try:
                with Image.open(target) as opened:
                    opened.load()
                    return _to_rgb(opened)
            except (OSError, Image.DecompressionBombError) as exc:
                raise CaptureUnavailableError(
                    CaptureStatus.CAPTURE_FAILED, _UNREADABLE
                ) from exc

    def _to_rgb(image: Image.Image) -> Image.Image:
        """A detached RGB copy (the temp file is removed afterwards)."""
        if image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        ):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        return image.convert("RGB")

    def _contains(rect: Rect, x: float, y: float) -> bool:
        return rect.x <= x < rect.right and rect.y <= y < rect.bottom

    class _MacGrab:
        def grab_rect(self, rect: Rect) -> Image.Image:
            if rect.is_empty:
                raise CaptureUnavailableError(
                    CaptureStatus.CAPTURE_FAILED, "the area to capture is empty"
                )
            region = f"{rect.x},{rect.y},{rect.width},{rect.height}"
            return _screencapture(["-R", region])

        def grab_window_only(self, window: DesktopWindow) -> Image.Image:
            try:
                window_id = int(window.window_id)
            except ValueError as exc:
                raise CaptureUnavailableError(
                    CaptureStatus.CAPTURE_FAILED, "the window id is not valid"
                ) from exc
            if window.minimized or not cg_window_on_screen(window_id):
                raise CaptureUnavailableError(
                    CaptureStatus.WINDOW_OBSCURED, _NOT_ON_SCREEN
                )
            return _screencapture(["-o", "-l", str(window_id)])

        def is_unobscured(
            self, window: DesktopWindow, windows: Sequence[DesktopWindow]
        ) -> bool:
            if not unobscured_by_stacking(window, windows):
                return False
            # A window reaching past the displays' edges is only partly shown.
            return _covered_by_displays(window.rect, display_rects())

        def monitor_rect(self, window: DesktopWindow) -> Rect:
            displays = display_rects()
            if not displays:
                raise CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _NO_DISPLAY)
            best = max(displays, key=lambda d: d.intersection(window.rect).area)
            if best.intersection(window.rect).area > 0:
                return best
            main = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            return Rect(
                round(main.origin.x),
                round(main.origin.y),
                round(main.size.width),
                round(main.size.height),
            )

    def _covered_by_displays(rect: Rect, displays: Sequence[Rect]) -> bool:
        """Whether every sample point of ``rect`` lies on some display (a
        window spanning two monitors)."""
        for fy in _SAMPLE_FRACTIONS:
            for fx in _SAMPLE_FRACTIONS:
                x = rect.x + fx * rect.width
                y = rect.y + fy * rect.height
                if not any(_contains(display, x, y) for display in displays):
                    return False
        return True

    GRAB: WindowGrabBackend = _MacGrab()
