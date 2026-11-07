"""What each platform module of the window capture must provide.

The core (``window_inventory``,
``capture_session``, ``window_grab``, ``window_capture``) picks one platform
path with ``detect_capture_platform`` and talks to it only through the
Protocols below; the platform modules never import the core.

Convention: module-level objects with fixed names
------------------------------------------------
Every platform module exports its part as a module-level object with a fixed
name, the way ``desktop_gnome.BACKEND`` does for the agent's desktop:

* an inventory module exports ``INVENTORY: WindowInventoryBackend``;
* a grab module exports ``GRAB: WindowGrabBackend``;
* a session module exports ``SESSION: CaptureSessionBackend``;
* a desktop-switch module exports a function ``create_switcher`` matching
  ``desktop_switch.SwitcherFactory``
  (``(DesktopInfo, Sequence[DesktopWindow]) -> DesktopSwitcher``).

Annotate the exports with the Protocol type (``INVENTORY:
WindowInventoryBackend = _Win32Inventory()``) so mypy checks the class
against it. A module serving two platform paths prefixes the names with the
path (``SWAY_INVENTORY`` / ``HYPRLAND_INVENTORY``, ...).

=====================  ===============================  ============================
Platform path          Module                           Exports
=====================  ===============================  ============================
WINDOWS                ``win32_windows``                ``INVENTORY``
                       ``window_grab_windows``          ``GRAB``
                       ``capture_session_windows``      ``SESSION``
                       ``win32_desktop_switch``         ``create_switcher``
                       ``win32_virtual_desktops``       (helpers for the above)
MACOS                  ``window_inventory_macos``       ``INVENTORY``
                       ``window_grab_macos``            ``GRAB``
                       ``capture_session_macos``        ``SESSION``
                       ``macos_desktop_switch``         ``create_switcher``
X11                    ``window_inventory_x11``         ``INVENTORY``
                       ``window_grab_x11``              ``GRAB``
                       ``x11_desktop_switch``           ``create_switcher``
                       ``x11_lib``                      (ctypes bindings)
SWAY, HYPRLAND         ``wayland_wlroots``              ``SWAY_INVENTORY``,
                                                        ``SWAY_GRAB``,
                                                        ``create_sway_switcher``,
                                                        ``HYPRLAND_INVENTORY``,
                                                        ``HYPRLAND_GRAB``,
                                                        ``create_hyprland_switcher``
GNOME_WAYLAND,         ``gnome_shell_capture``          ``INVENTORY``, ``GRAB``,
GNOME_X11                                               ``create_switcher``
every Linux path       ``capture_session_linux``        ``SESSION``
=====================  ===============================  ============================

(All modules are ``controller_client.<name>``.) ``capture_session_linux``
covers every Linux path, including NO_DISPLAY, KDE_WAYLAND and
OTHER_WAYLAND (it calls ``detect_capture_platform`` itself). The GNOME
helper's absence or age is reported by ``gnome_shell_capture``
(``helper_missing`` from any of its methods; GNOME on Xorg never falls back
to the plain X11 path, whose root grab would show the Shell's notification
banners and dialogs: they are drawn in the compositor, not as X windows), a missing ``swaymsg`` / ``hyprctl`` / ``grim`` by
``wayland_wlroots``.

Import rules
------------
* Importing a module does no I/O: no display, D-Bus or COM connection, no
  subprocess, no thread. Build the exported object cheaply; connect inside
  the methods.
* The core imports Windows modules only under ``sys.platform == "win32"``,
  macOS modules only under ``"darwin"`` and the Linux modules (all of them,
  whatever the session) under ``"linux"``. So a Linux module must import on
  any Linux machine, without ``DISPLAY``, ``WAYLAND_DISPLAY``, libX11 or a
  session bus. Guard platform-only imports with ``sys.platform`` as
  ``app_closer_windows.py`` / ``manual_hotkeys_x11.py`` do, and keep the
  module type-checking on every host (mypy runs on macOS too).
* No new pip dependency: ctypes, psutil, Pillow, pyobjc (macOS, via
  pyautogui), ``dbus_fast`` (Linux) and system tools only.

Behaviour every implementation shares
-------------------------------------
* Every method blocks and runs on the capture worker thread
  (``asyncio.to_thread``), never on the event loop; per-thread resources
  (COM, an X ``Display``, a D-Bus loop thread) are set up per call or per
  thread and always released.
* Expected refusals raise ``window_model.CaptureUnavailableError(status,
  detail)`` with a fixed, user-safe ``detail`` (no window titles); the
  service turns it into the reply. Any other exception is a bug and becomes
  ``capture_failed``.
* Images are ``PIL.Image.Image`` in mode ``RGB`` (convert ``RGBA`` / ``BGRX``
  first), in physical pixels; they may be larger than their rect on HiDPI.
* Logs carry status, process, size and desktop index only, never titles.
* The inventory reports every visible window of the current desktop, not
  only app windows: overlays, panels of apps, tool and owned windows,
  toasts and higher macOS layers come with ``capturable=False`` (see
  ``window_model.DesktopWindow``), and so does any window without a known
  pid. Only the OS shell's own taskbar, menu bar and Dock (Windows
  ``Shell_TrayWnd`` / ``Shell_SecondaryTrayWnd`` / ``Progman`` / ``WorkerW``;
  the macOS menu bar and the Dock's full-display window; Linux dock and
  desktop windows) and the wallpaper are left out.
* ``is_unobscured`` uses ``window_capture_policy.unobscured_by_stacking``
  (rectangle intersection over that full list); a platform may only add
  checks to it (macOS: the window lies on the displays).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from PIL import Image

from controller_client.desktop_switch import SwitcherFactory
from controller_client.protocol import CaptureStatus
from controller_client.window_model import DesktopInfo, DesktopWindow, Rect

# User-safe details of the paths that cannot capture (the session check and
# the inventory dispatcher share them).
COMPOSITOR_UNSUPPORTED_DETAIL: Final[str] = (
    "this desktop session cannot be captured; log in to a Plasma (X11) "
    "session, GNOME, Sway or Hyprland"
)
NO_DISPLAY_DETAIL: Final[str] = (
    "the Test Runner is not running inside a logged-in desktop session"
)
UNSUPPORTED_OS_DETAIL: Final[str] = "window capture is not available on this system"


class CapturePlatform(StrEnum):
    """The capture path of this session (``detect_capture_platform``)."""

    WINDOWS = "windows"
    MACOS = "macos"
    # Any EWMH window manager on Xorg (KDE/Xfce/Cinnamon/... on X11; GNOME
    # Shell on Xorg is GNOME_X11).
    X11 = "x11"
    SWAY = "sway"
    HYPRLAND = "hyprland"
    # GNOME Shell on Wayland, through the bundled helper extension.
    GNOME_WAYLAND = "gnome_wayland"
    # GNOME Shell on Xorg, through the same helper extension (its banners,
    # dialogs and overview are drawn in the compositor stage).
    GNOME_X11 = "gnome_x11"
    # Refused with ``compositor_unsupported``.
    KDE_WAYLAND = "kde_wayland"
    OTHER_WAYLAND = "other_wayland"
    # Linux without DISPLAY or WAYLAND_DISPLAY: ``not_interactive``.
    NO_DISPLAY = "no_display"
    # Any other OS: ``unsupported``.
    UNSUPPORTED_OS = "unsupported_os"


# The paths that have platform modules (inventory, grab, switcher).
SUPPORTED_CAPTURE_PLATFORMS: frozenset[CapturePlatform] = frozenset(
    {
        CapturePlatform.WINDOWS,
        CapturePlatform.MACOS,
        CapturePlatform.X11,
        CapturePlatform.SWAY,
        CapturePlatform.HYPRLAND,
        CapturePlatform.GNOME_WAYLAND,
        CapturePlatform.GNOME_X11,
    }
)

# XDG_CURRENT_DESKTOP entries of desktops that name GNOME but do not run
# GNOME Shell (Budgie, GNOME Flashback, ...): on Xorg they take the X11 path.
_NOT_GNOME_SHELL: Final[frozenset[str]] = frozenset(
    {
        "budgie",
        "cinnamon",
        "x-cinnamon",
        "gnome-flashback",
        "pantheon",
        "unity",
        "mate",
        "xfce",
        "lxde",
        "lxqt",
        "kde",
    }
)


def _desktop_names(environ: Mapping[str, str]) -> tuple[str, ...]:
    raw = environ.get("XDG_CURRENT_DESKTOP", "")
    return tuple(part.strip().lower() for part in raw.split(":") if part.strip())


def detect_capture_platform(
    platform: str = sys.platform, environ: Mapping[str, str] | None = None
) -> CapturePlatform:
    """The capture path for ``platform`` (``sys.platform``) and ``environ``.

    ``environ`` defaults to ``os.environ`` (pass a mapping in diagnostics).
    Linux: a Wayland session (``XDG_SESSION_TYPE=wayland``, or
    ``WAYLAND_DISPLAY`` set without ``XDG_SESSION_TYPE=x11``) is SWAY when
    ``SWAYSOCK`` is set, HYPRLAND when ``HYPRLAND_INSTANCE_SIGNATURE`` is set,
    GNOME_WAYLAND when ``XDG_CURRENT_DESKTOP`` names GNOME, KDE_WAYLAND when
    it names KDE, else OTHER_WAYLAND; otherwise, with ``DISPLAY`` set,
    GNOME_X11 when ``XDG_CURRENT_DESKTOP`` names GNOME and no desktop known
    not to run GNOME Shell (Budgie, GNOME Flashback, ...), else X11; else
    NO_DISPLAY. The one definition the session check and the inventory
    dispatcher share.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    if platform == "win32":
        return CapturePlatform.WINDOWS
    if platform == "darwin":
        return CapturePlatform.MACOS
    if not platform.startswith("linux"):
        return CapturePlatform.UNSUPPORTED_OS
    session_type = env.get("XDG_SESSION_TYPE", "").strip().lower()
    wayland = session_type == "wayland" or (
        bool(env.get("WAYLAND_DISPLAY")) and session_type != "x11"
    )
    if wayland:
        desktops = _desktop_names(env)
        if env.get("SWAYSOCK"):
            return CapturePlatform.SWAY
        if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return CapturePlatform.HYPRLAND
        if "gnome" in desktops:
            return CapturePlatform.GNOME_WAYLAND
        if "kde" in desktops:
            return CapturePlatform.KDE_WAYLAND
        return CapturePlatform.OTHER_WAYLAND
    if env.get("DISPLAY"):
        desktops = _desktop_names(env)
        if "gnome" in desktops and not _NOT_GNOME_SHELL.intersection(desktops):
            return CapturePlatform.GNOME_X11
        return CapturePlatform.X11
    return CapturePlatform.NO_DISPLAY


class WindowInventoryBackend(Protocol):
    """Lists the session's top-level app windows on every desktop."""

    def list_windows(self) -> tuple[DesktopInfo, list[DesktopWindow]]:
        """The desktops and every app window (allowlisted or not, minimised
        or not, on any desktop), plus every other visible window of the
        current desktop with ``capturable=False`` (module docstring), sorted
        topmost first (``z`` 0, 1, 2, ...).

        Rects are in this platform's desktop coordinates (``window_model``).
        Processes are normalized (``normalize_process_name``; "" when
        unknown). Raises ``CaptureUnavailableError`` (for example
        ``helper_missing``, ``compositor_unsupported``, ``permission_missing``).
        """
        ...


class WindowGrabBackend(Protocol):
    """Turns a window or a desktop rect into an image."""

    def grab_rect(self, rect: Rect) -> Image.Image:
        """What a person sees in ``rect`` (desktop coordinates, may span
        monitors): every window there, the app's own menus and tooltips
        included. Raises ``CaptureUnavailableError`` (``capture_failed``,
        ``permission_missing``, ...)."""
        ...

    def grab_window_only(self, window: DesktopWindow) -> Image.Image:
        """``window``'s own current content without any other window
        (Windows ``PrintWindow(PW_RENDERFULLCONTENT)``, macOS ``screencapture
        -l``, X11 Composite pixmap, GNOME actor content), used when it is
        obscured. Must not return a stale or unpainted frame it can detect.
        Raises ``CaptureUnavailableError(WINDOW_OBSCURED, ...)`` when this
        platform or session has no faithful window-only path (wlroots, X11
        without a compositing manager, macOS for a window not on screen)."""
        ...

    def is_unobscured(
        self, window: DesktopWindow, windows: Sequence[DesktopWindow]
    ) -> bool:
        """Whether ``window`` is fully visible on the current desktop: no
        visible window of another process stacked above it overlaps its rect
        (``window_capture_policy.unobscured_by_stacking`` over ``windows``,
        the full inventory it came from, topmost first). False for a
        minimised or undrawn window or one on another desktop."""
        ...

    def monitor_rect(self, window: DesktopWindow) -> Rect:
        """The monitor holding most of ``window``'s rect (desktop
        coordinates); the primary monitor when it is on none. The ``screen``
        target grabs this rect. Raises ``CaptureUnavailableError``."""
        ...


class CaptureSessionBackend(Protocol):
    """Checks that the session can show and capture windows."""

    def problem(self) -> tuple[CaptureStatus, str] | None:
        """None when listing and capturing can go ahead; otherwise the status
        (``not_interactive``, ``permission_missing``,
        ``compositor_unsupported``) and its fixed, user-safe detail. Checked
        before every list and every capture; must be quick (well under a
        second) and never raise."""
        ...


@dataclass(frozen=True)
class PlatformCapture:
    """One platform path's parts, as the core assembles them from the
    module exports above (the session check is separate: ``SESSION``)."""

    platform: CapturePlatform
    inventory: WindowInventoryBackend
    grab: WindowGrabBackend
    create_switcher: SwitcherFactory
