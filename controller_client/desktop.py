"""Pick the desktop backend for this platform.

Linux test machines run GNOME on Wayland, where pyautogui cannot capture or
drive native windows (and importing it crashes without an X display), so
pyautogui is imported only on the other platforms.
"""

from __future__ import annotations

import sys

from controller_client.desktop_backend import DesktopBackend

if sys.platform == "linux":
    from controller_client.desktop_gnome import BACKEND as _BACKEND
else:
    from controller_client.desktop_pyautogui import BACKEND as _BACKEND


def get_desktop_backend() -> DesktopBackend:
    return _BACKEND
