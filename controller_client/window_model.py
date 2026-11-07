"""Plain data the window capture platform modules return.

No platform imports and no I/O: the inventory, grab, session and
desktop-switch modules of every platform (``capture_platform.py``) build
these, and the pure policy (``window_capture_policy.py``) and the service
(``window_capture.py``) read them.

Coordinates: every ``Rect`` of one inventory is in that platform's *desktop
coordinate space*, the space its grab backend's ``grab_rect`` takes (Windows:
physical virtual-screen pixels, read per-monitor DPI aware, the space of
``ImageGrab.grab(all_screens=True)``; macOS: global
display points, top-left origin, as ``screencapture -R`` takes them; X11: root
window pixels; Sway / Hyprland: layout coordinates as ``grim -g`` takes them;
GNOME: stage pixels). It may span several monitors and have negative origins.
Images may have more pixels than their rect (HiDPI); regions are applied to
image pixels by the service, never to rects.
"""

from __future__ import annotations

from dataclasses import dataclass

from controller_client.protocol import CaptureStatus


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle; ``width`` / ``height`` may be 0 (empty)."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        """One past the last column."""
        return self.x + self.width

    @property
    def bottom(self) -> int:
        """One past the last row."""
        return self.y + self.height

    @property
    def area(self) -> int:
        return max(self.width, 0) * max(self.height, 0)

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def contains_point(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def intersection(self, other: Rect) -> Rect:
        """The overlap of both rects (empty, size 0, when they do not meet)."""
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return Rect(left, top, 0, 0)
        return Rect(left, top, right - left, bottom - top)

    def intersects(self, other: Rect) -> bool:
        return not self.intersection(other).is_empty


@dataclass(frozen=True)
class WindowDesktop:
    """The virtual desktop a window is on.

    ``current``: shown now (sticky / all-desktop windows count as current).
    ``index``: 1-based, None where the OS does not expose it (macOS Spaces).
    ``name``: None when unnamed or unknown.
    """

    current: bool
    index: int | None
    name: str | None


@dataclass(frozen=True)
class DesktopWindow:
    """One top-level window, on any desktop.

    The inventory lists every app window (allowlisted or not, minimised or
    not, on every desktop) plus the other visible windows of the current
    desktop (overlays, panels of apps, tool and owned windows, toasts, higher
    macOS layers), flagged ``capturable=False``. Filtering is the policy's
    job. It leaves out only the wallpaper / desktop window, the OS shell's
    own taskbar, menu bar and Dock, and invisible and zero-size windows.

    ``capturable`` windows may be selected, listed and allowlisted; every
    listed window, capturable or not, counts for the obscured check and the
    screen rule.
    """

    # The platform's own id as a string (decimal HWND, CGWindowID, X11 window
    # id, sway con id, Hyprland address, GNOME ``meta_window.get_id()``); at
    # most CAPTURE_WINDOW_ID_MAX_CHARS. Stable across two enumerations of the
    # same window, so the service can find it again after a desktop switch.
    window_id: str
    # Owning process id; None when the platform does not report it.
    pid: int | None
    # ``normalize_process_name`` of the process (Windows: exe basename;
    # macOS: owner name; Linux: psutil name of the pid, or the wlroots
    # app id / class when there is no pid); "" when unknown, which never
    # matches an allowlist entry.
    process: str
    # Raw title (may be ""); never logged.
    title: str
    # Outer frame in desktop coordinates (see the module docstring).
    rect: Rect
    minimized: bool
    # Holds the keyboard focus (the foreground / active window).
    focused: bool
    # Stacking position: 0 is topmost, larger is further back; unique within
    # one inventory. Windows on other desktops are ordered after the current
    # desktop's where the platform gives no global order.
    z: int
    desktop: WindowDesktop
    # Hidden by the app or OS itself, not by a desktop switch (Windows:
    # DWM_CLOAKED_APP, a suspended UWP window); never captured. False
    # elsewhere.
    app_cloaked: bool = False
    # A normal app window with a known owning process: may be selected,
    # listed and matched against the allowlist. False for an overlay, a
    # panel, a tool or owned window, a toast, a higher macOS layer, and for
    # any window whose pid the platform does not report (its app id or class
    # is chosen by the client itself, so it proves nothing).
    capturable: bool = True
    # Drawn on screen when its desktop is shown. False for a window that is
    # listed on a shown desktop but not painted (a background sway tab): it
    # covers nothing and is never grabbed from the screen.
    drawn: bool = True


@dataclass(frozen=True)
class DesktopInfo:
    """The virtual desktops of the session.

    ``current`` (1-based) and ``count`` are None where the OS does not expose
    them (macOS). ``names[i]`` is the name of desktop ``i + 1`` ("" when that
    one is unnamed); empty when no names are known.
    """

    current: int | None
    count: int | None
    names: tuple[str, ...] = ()


class CaptureUnavailableError(Exception):
    """A list or capture cannot be done; the service replies with ``status``.

    Raised by every platform module for an expected, user-explainable
    refusal: ``status`` is the wire status (never OK), ``detail`` is plain
    user-safe text (no window titles), at most CAPTURE_DETAIL_MAX_CHARS.
    """

    def __init__(self, status: CaptureStatus, detail: str) -> None:
        super().__init__(f"{status.value}: {detail}")
        self.status = status
        self.detail = detail
