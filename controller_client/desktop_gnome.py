"""The desktop backend for GNOME on Wayland (Linux).

Input goes through Mutter's RemoteDesktop session and frames come from the
linked ScreenCast stream of the primary monitor. Frames are in physical pixels
and Mutter maps stream coordinates onto the monitor itself, so screenshot
coordinates are passed to input unchanged. Constructing the backend touches
neither D-Bus nor any process: the session starts on first use.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from typing import Final

from PIL import Image

from controller_client.desktop_backend import DesktopBackend
from controller_client.diagnostics import DiagnosticStep
from controller_client.exceptions import (
    DesktopSessionError,
    ExecutionError,
    ScreenCaptureError,
)
from controller_client.gnome_diagnostics import build_gnome_steps
from controller_client.gnome_remote_session import (
    BTN_LEFT,
    BTN_MIDDLE,
    BTN_RIGHT,
    GnomeRemoteSession,
)
from controller_client.keysyms import (
    char_to_keysym,
    key_name_to_keysym,
    resolve_key_name,
)
from controller_client.pipewire_frame import grab_frame

logger = logging.getLogger(__name__)

_FRAME_TIMEOUT_S: Final[float] = 10.0
# Well inside GNOME's default 400 ms double-click time.
_DOUBLE_CLICK_GAP_S: Final[float] = 0.05
_DRAG_STEPS_PER_SECOND: Final[int] = 60
_DRAG_MIN_STEPS: Final[int] = 5

_BUTTONS: Final[dict[str, int]] = {
    "left": BTN_LEFT,
    "right": BTN_RIGHT,
    "middle": BTN_MIDDLE,
}


class GnomeDesktopBackend:
    def __init__(self) -> None:
        self._session = GnomeRemoteSession()
        self._size_lock = threading.Lock()
        # The latest frame's size and the stream it came from: a rebuilt
        # session (monitor or scale change) makes it stale.
        self._frame_size: tuple[int, int] | None = None
        self._frame_stream: str | None = None

    @property
    def name(self) -> str:
        return "gnome-wayland"

    # Each action sends its events on the session it checked the bounds on
    # (``stream_path``): see GnomeRemoteSession._session_for. A release goes
    # out even when its press failed, since a press whose reply timed out may
    # still have reached mutter; a failed release never hides the press error.

    def click(self, x: int, y: int, button: str) -> None:
        code = _button_code(button)
        stream = self._check_bounds(x, y)
        self._session.pointer_move(x, y, stream_path=stream)
        self._press_release(code, stream)

    def double_click(self, x: int, y: int) -> None:
        stream = self._check_bounds(x, y)
        self._session.pointer_move(x, y, stream_path=stream)
        self._press_release(BTN_LEFT, stream)
        time.sleep(_DOUBLE_CLICK_GAP_S)
        self._press_release(BTN_LEFT, stream)

    def move_to(self, x: int, y: int) -> None:
        stream = self._check_bounds(x, y)
        self._session.pointer_move(x, y, stream_path=stream)

    def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        duration: float,
    ) -> None:
        code = _button_code(button)
        stream = self._check_bounds(start_x, start_y)
        if self._check_bounds(end_x, end_y) != stream:
            raise ExecutionError(
                "The screen changed while the drag was being prepared. Take a "
                "new screenshot and retry."
            )
        steps = max(_DRAG_MIN_STEPS, round(duration * _DRAG_STEPS_PER_SECOND))
        step_sleep = max(duration, 0.0) / steps
        self._session.pointer_move(start_x, start_y, stream_path=stream)

        def press_and_move() -> None:
            self._session.pointer_button(code, True, stream_path=stream)
            for step in range(1, steps + 1):
                fraction = step / steps
                self._session.pointer_move(
                    start_x + (end_x - start_x) * fraction,
                    start_y + (end_y - start_y) * fraction,
                    stream_path=stream,
                )
                if step_sleep > 0:
                    time.sleep(step_sleep)

        _then_release(
            press_and_move, lambda quiet: self._release_button(code, stream, quiet)
        )

    def type_text(self, text: str, interval: float) -> None:
        stream = self._session.ensure_started().stream_path
        for index, char in enumerate(text):
            keysym = char_to_keysym(char)
            _then_release(
                lambda: self._session.key(keysym, True, stream_path=stream),
                lambda quiet: self._release_keys([keysym], stream, quiet),
            )
            if interval > 0 and index < len(text) - 1:
                time.sleep(interval)

    def resolve_key(self, name: str) -> str | None:
        return resolve_key_name(name)

    def press_keys(self, keys: Sequence[str]) -> None:
        keysyms = [key_name_to_keysym(key) for key in keys]
        stream = self._session.ensure_started().stream_path
        pressed: list[int] = []

        def press_all() -> None:
            for keysym in keysyms:
                pressed.append(keysym)
                self._session.key(keysym, True, stream_path=stream)

        _then_release(
            press_all,
            lambda quiet: self._release_keys(list(reversed(pressed)), stream, quiet),
        )

    def screenshot(self) -> Image.Image:
        image, _ = self._capture()
        return image

    def screen_size(self) -> tuple[int, int]:
        # No capture here: it runs in the handshake, which must not wait on a
        # locked screen. The startup capture check has normally set it.
        with self._size_lock:
            size = self._frame_size
        if size is None:
            logger.warning("Screen size unknown: no screenshot has succeeded yet")
            return (0, 0)
        return size

    def session_info(self) -> dict[str, str]:
        return {
            "display_server": os.environ.get("XDG_SESSION_TYPE") or "unknown",
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP") or "unknown",
        }

    def diagnostic_steps(self) -> tuple[DiagnosticStep, ...]:
        return build_gnome_steps(self._session, self.screenshot)

    def _known_frame_size(self) -> tuple[str, tuple[int, int]]:
        """The current session's stream path and its screen size in pixels."""
        stream = self._session.ensure_started().stream_path
        with self._size_lock:
            size = self._frame_size
            current = self._frame_stream == stream
        if size is not None and current:
            return stream, size
        image, frame_stream = self._capture()
        return frame_stream, image.size

    def _capture(self) -> tuple[Image.Image, str]:
        """A frame and the stream it came from; the pair is recorded together."""
        try:
            stream = self._session.ensure_started()
        except DesktopSessionError as e:
            raise ScreenCaptureError(str(e)) from e
        image = grab_frame(stream.node_id, _FRAME_TIMEOUT_S)
        with self._size_lock:
            self._frame_size = image.size
            self._frame_stream = stream.stream_path
        return image, stream.stream_path

    def _check_bounds(self, x: int, y: int) -> str:
        """Check the point against the screen; return the session's stream path."""
        stream, (width, height) = self._known_frame_size()
        if not (0 <= x < width and 0 <= y < height):
            raise ExecutionError(
                f"Coordinates ({x}, {y}) are outside the screen, which is "
                f"{width}x{height} pixels as in the latest screenshot. Use "
                "coordinates from a current screenshot."
            )
        return stream

    def _press_release(self, code: int, stream: str) -> None:
        _then_release(
            lambda: self._session.pointer_button(code, True, stream_path=stream),
            lambda quiet: self._release_button(code, stream, quiet),
        )

    def _release_button(self, code: int, stream: str, quiet: bool) -> None:
        # A closed session took its virtual device, and anything it held,
        # with it; there is nothing left to release.
        if self._session.active_stream_path() != stream:
            return
        try:
            self._session.pointer_button(code, False, stream_path=stream)
        except DesktopSessionError as e:
            if not quiet:
                raise
            logger.warning("Mouse button release failed: %s", e)

    def _release_keys(self, keysyms: Sequence[int], stream: str, quiet: bool) -> None:
        """Release every key, even after one release fails."""
        if self._session.active_stream_path() != stream:
            return
        first_error: DesktopSessionError | None = None
        for keysym in keysyms:
            try:
                self._session.key(keysym, False, stream_path=stream)
            except DesktopSessionError as e:
                first_error = first_error or e
        if first_error is None:
            return
        if not quiet:
            raise first_error
        logger.warning("Key release failed: %s", first_error)


def _then_release(action: Callable[[], None], release: Callable[[bool], None]) -> None:
    """Run a press action, then its release (quiet when the press failed)."""
    try:
        action()
    except BaseException:
        release(True)
        raise
    release(False)


def _button_code(button: str) -> int:
    code = _BUTTONS.get(button)
    if code is None:
        raise ExecutionError(
            f"Unknown mouse button {button!r}; use 'left', 'right' or 'middle'."
        )
    return code


BACKEND: Final[DesktopBackend] = GnomeDesktopBackend()
