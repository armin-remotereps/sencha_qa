"""Pick this session's capture platform modules and list its windows.

The platform
path comes from ``capture_platform.detect_capture_platform``; the modules of
each path follow the export convention in ``capture_platform.py`` and are
imported only on their OS (``sys.platform`` guards at module top, like
``app_closer.py``). KDE Wayland and unknown Wayland compositors are refused
with ``compositor_unsupported``, a Linux session without a display with
``not_interactive``.
"""

from __future__ import annotations

import sys

from controller_client.capture_platform import (
    COMPOSITOR_UNSUPPORTED_DETAIL,
    NO_DISPLAY_DETAIL,
    UNSUPPORTED_OS_DETAIL,
    CapturePlatform,
    PlatformCapture,
    detect_capture_platform,
)
from controller_client.protocol import CaptureStatus
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopInfo,
    DesktopWindow,
)

if sys.platform == "win32":
    from controller_client import (
        win32_desktop_switch,
        win32_windows,
        window_grab_windows,
    )
elif sys.platform == "darwin":
    from controller_client import (
        macos_desktop_switch,
        window_grab_macos,
        window_inventory_macos,
    )
elif sys.platform.startswith("linux"):
    from controller_client import (
        gnome_shell_capture,
        wayland_wlroots,
        window_grab_x11,
        window_inventory_x11,
        x11_desktop_switch,
    )
    from controller_client.manual_hotkeys_x11 import prepare_x11_threads

if sys.platform == "win32":

    def _platform_parts(platform: CapturePlatform) -> PlatformCapture | None:
        if platform is not CapturePlatform.WINDOWS:
            return None
        return PlatformCapture(
            platform=platform,
            inventory=win32_windows.INVENTORY,
            grab=window_grab_windows.GRAB,
            create_switcher=win32_desktop_switch.create_switcher,
        )

elif sys.platform == "darwin":

    def _platform_parts(platform: CapturePlatform) -> PlatformCapture | None:
        if platform is not CapturePlatform.MACOS:
            return None
        return PlatformCapture(
            platform=platform,
            inventory=window_inventory_macos.INVENTORY,
            grab=window_grab_macos.GRAB,
            create_switcher=macos_desktop_switch.create_switcher,
        )

elif sys.platform.startswith("linux"):

    def _platform_parts(platform: CapturePlatform) -> PlatformCapture | None:
        if platform is CapturePlatform.X11:
            # Each X11 call opens its own Display on the worker thread;
            # libX11 < 1.8 needs XInitThreads first (idempotent, no I/O).
            prepare_x11_threads()
            return PlatformCapture(
                platform=platform,
                inventory=window_inventory_x11.INVENTORY,
                grab=window_grab_x11.GRAB,
                create_switcher=x11_desktop_switch.create_switcher,
            )
        if platform is CapturePlatform.SWAY:
            return PlatformCapture(
                platform=platform,
                inventory=wayland_wlroots.SWAY_INVENTORY,
                grab=wayland_wlroots.SWAY_GRAB,
                create_switcher=wayland_wlroots.create_sway_switcher,
            )
        if platform is CapturePlatform.HYPRLAND:
            return PlatformCapture(
                platform=platform,
                inventory=wayland_wlroots.HYPRLAND_INVENTORY,
                grab=wayland_wlroots.HYPRLAND_GRAB,
                create_switcher=wayland_wlroots.create_hyprland_switcher,
            )
        if platform in (CapturePlatform.GNOME_WAYLAND, CapturePlatform.GNOME_X11):
            # GNOME on Xorg too: never the plain X11 path (its root grab
            # would show the Shell's banners and dialogs, which are not X
            # windows); without the helper every call is helper_missing.
            return PlatformCapture(
                platform=platform,
                inventory=gnome_shell_capture.INVENTORY,
                grab=gnome_shell_capture.GRAB,
                create_switcher=gnome_shell_capture.create_switcher,
            )
        return None

else:

    def _platform_parts(platform: CapturePlatform) -> PlatformCapture | None:
        return None


def platform_capture(platform: CapturePlatform | None = None) -> PlatformCapture:
    """The inventory, grab and switcher of ``platform`` (default: this
    session's path). Cheap (no I/O). Raises ``CaptureUnavailableError`` for
    a path that cannot capture."""
    path = detect_capture_platform() if platform is None else platform
    parts = _platform_parts(path)
    if parts is not None:
        return parts
    if path is CapturePlatform.NO_DISPLAY:
        raise CaptureUnavailableError(CaptureStatus.NOT_INTERACTIVE, NO_DISPLAY_DETAIL)
    if path in (CapturePlatform.KDE_WAYLAND, CapturePlatform.OTHER_WAYLAND):
        raise CaptureUnavailableError(
            CaptureStatus.COMPOSITOR_UNSUPPORTED, COMPOSITOR_UNSUPPORTED_DETAIL
        )
    raise CaptureUnavailableError(CaptureStatus.UNSUPPORTED, UNSUPPORTED_OS_DETAIL)


def list_desktop_windows() -> tuple[DesktopInfo, list[DesktopWindow]]:
    """The session's desktops and every app window on every desktop, topmost
    first (blocking; worker thread). Raises ``CaptureUnavailableError``."""
    return platform_capture().inventory.list_windows()
