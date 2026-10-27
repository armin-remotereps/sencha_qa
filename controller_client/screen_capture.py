"""Screen capture through this platform's desktop backend.

The capture itself lives in the backend (``desktop_pyautogui`` on Windows and
macOS, ``desktop_gnome`` on GNOME Wayland); this module keeps the entry points
the executor and the startup checks call.
"""

from __future__ import annotations

from PIL import Image

from controller_client.desktop import get_desktop_backend
from controller_client.exceptions import ScreenCaptureError


def capture_screen() -> Image.Image:
    """Capture the whole screen, or raise ScreenCaptureError explaining why not."""
    return get_desktop_backend().screenshot()


def describe_screen_capture_failure() -> str | None:
    """Return why a screen capture would fail right now, or None if it works."""
    try:
        capture_screen()
    except ScreenCaptureError as exc:
        return str(exc)
    return None
