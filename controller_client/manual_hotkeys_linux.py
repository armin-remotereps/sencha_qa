"""Pick the Linux backend of the Manual run shortcuts by session type.

Wayland uses the GlobalShortcuts portal; X11 grabs the keys on the root
window. XGrabKey is never used on Wayland: an XWayland grab only sees the key
presses that go to X clients, so it would work only while an X window is in
front. Anything else gets the window's own (focus-only) bindings.

``dbus_fast`` is a Linux-only requirement here (not installed on Windows), so
the portal module is imported on Linux only, as ``desktop.py`` does.
"""

import os
import sys

from controller_client.manual_hotkeys_base import (
    HotkeyBackend,
    HotkeyQueue,
    StaticBackend,
    focus_only,
)
from controller_client.manual_hotkeys_x11 import X11Hotkeys

if sys.platform == "linux":
    from controller_client.manual_hotkeys_portal import PortalHotkeys

_NO_SERVICE = "No global shortcut service on this desktop"


def session_type() -> str:
    """``wayland``, ``x11`` or ``""`` for this desktop session."""
    declared = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in ("wayland", "x11"):
        return declared
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return ""


def select_backend(
    events: HotkeyQueue, *, x11_window_id: int, dry_run: bool
) -> HotkeyBackend:
    kind = session_type() if sys.platform == "linux" else ""
    # The platform check repeats here so mypy skips the branch elsewhere.
    if sys.platform == "linux" and kind == "wayland":
        return PortalHotkeys(events, x11_window_id=x11_window_id, dry_run=dry_run)
    if kind == "x11":
        return X11Hotkeys(events, dry_run=dry_run)
    return StaticBackend(focus_only(_NO_SERVICE))
