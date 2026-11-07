"""X11 window inventory through EWMH (any EWMH window manager on Xorg).

One ``Display`` per call (``x11_lib.open_display``):

* windows from ``_NET_CLIENT_LIST_STACKING`` (bottom to top; a window manager
  without it, or without ``_NET_SUPPORTING_WM_CHECK``, is
  ``compositor_unsupported``: the obscured check needs the stacking order);
* app windows (``capturable``): ``_NET_WM_WINDOW_TYPE`` normal or dialog (or
  unset) with a ``_NET_WM_PID``; desktop and dock windows are left out only
  when their process is a known shell, panel or desktop
  (``shell_processes.LINUX_SHELL_SURFACE_PROCESSES``: any client can set
  the type); every other managed type (menus, tool windows, splashes,
  notifications, other docks) and any window without a pid on the current
  desktop is listed with ``capturable=False``, as are the mapped
  override-redirect windows (popup menus, tooltips, toasts, the window
  manager's own switchers and on-screen displays): every one, placed by
  the root window's stacking order (``XQueryTree``); those below the apps
  get a higher ``z``, so only the screen rule counts them;
  zero-size windows are left out;
* pid from ``_NET_WM_PID`` mapped to a process name through psutil
  (``process_names``);
* title from ``_NET_WM_NAME``, falling back to ``WM_NAME``;
* outer rect from ``XGetWindowAttributes`` + ``XTranslateCoordinates`` plus
  ``_NET_FRAME_EXTENTS`` (server-side decorations), minus
  ``_GTK_FRAME_EXTENTS`` (the invisible shadow of client-side decorations);
* minimised from ``_NET_WM_STATE_HIDDEN``; focused from
  ``_NET_ACTIVE_WINDOW``;
* desktop from ``_NET_WM_DESKTOP`` versus ``_NET_CURRENT_DESKTOP`` (sticky,
  ``0xFFFFFFFF`` or ``_NET_WM_STATE_STICKY``, counts as current), with
  ``_NET_NUMBER_OF_DESKTOPS`` and ``_NET_DESKTOP_NAMES``.

Window ids are the client window's X id in decimal. Rects are root window
pixels. ``z`` follows the global stacking order, current desktop first.

``shell_draws_overlays``: whether the window manager is a compositing shell
that draws its notification banners, modal dialogs and overview in its own
stage rather than as X windows (Cinnamon; GNOME Shell when it reaches this
path). Those never appear in the inventory, so ``window_grab_x11`` refuses
``screen`` and grabs windows only through Composite there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Final

from controller_client.capture_platform import WindowInventoryBackend
from controller_client.process_names import ProcessNames
from controller_client.protocol import CaptureStatus
from controller_client.shell_processes import LINUX_SHELL_SURFACE_PROCESSES
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopInfo,
    DesktopWindow,
    Rect,
    WindowDesktop,
)
from controller_client.x11_lib import (
    CARDINAL_MASK,
    EWMH_DETAIL,
    X11Display,
    open_display,
)

logger = logging.getLogger(__name__)

_APP_TYPES: Final = (
    "_NET_WM_WINDOW_TYPE_NORMAL",
    "_NET_WM_WINDOW_TYPE_DIALOG",
)
# Panels, docks and the desktop: left out only for a known shell process
# (``_is_shell_surface``), else overlays.
_SHELL_TYPES: Final = (
    "_NET_WM_WINDOW_TYPE_DOCK",
    "_NET_WM_WINDOW_TYPE_DESKTOP",
)
_TREE_DETAIL: Final = "the X window stacking order could not be read"
_STATE_ATOMS: Final = ("_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_STICKY")
_ALL_DESKTOPS: Final = CARDINAL_MASK
# Window manager processes (``_NET_SUPPORTING_WM_CHECK`` -> ``_NET_WM_PID``)
# that draw notifications and dialogs in their compositor stage.
_STAGE_OVERLAY_SHELLS: Final = frozenset({"cinnamon", "gnome-shell"})
# The same shells by the check window's ``_NET_WM_NAME`` (lower-case
# substrings), for a window manager that sets no pid there: Cinnamon's
# "Mutter (Muffin)", "GNOME Shell".
_STAGE_OVERLAY_WM_NAMES: Final = ("muffin", "gnome shell")


@dataclass(frozen=True)
class _Desktops:
    current: int | None  # 0-based
    count: int | None
    names: tuple[str, ...]


def _desktops(display: X11Display) -> _Desktops:
    current = display.cardinal(display.root, "_NET_CURRENT_DESKTOP")
    count = display.cardinal(display.root, "_NET_NUMBER_OF_DESKTOPS")
    names = tuple(display.utf8_list(display.root, "_NET_DESKTOP_NAMES"))
    if count is not None and count <= 0:
        count = None
    if count is not None:
        names = names[:count]
    if current is not None and count is not None and current >= count:
        current = None
    return _Desktops(current=current, count=count, names=names)


def _window_kind(display: X11Display, window: int) -> str:
    """ "app", "shell" or "overlay" from ``_NET_WM_WINDOW_TYPE``."""
    types = display.cardinals(window, "_NET_WM_WINDOW_TYPE")
    if not types:
        # No type: a normal top-level window (EWMH).
        return "app"
    # The first type the window manager understands wins; ours are known.
    first = types[0]
    if any(first == display.atom(name) for name in _APP_TYPES):
        return "app"
    if any(first == display.atom(name) for name in _SHELL_TYPES):
        return "shell"
    return "overlay"


def _is_shell_surface(pid: int | None, names: ProcessNames) -> bool:
    """Whether a dock or desktop window of ``pid`` is the shell's own (left
    out of the inventory). An unknown pid or process is not: its window is
    listed as a non-capturable overlay (fail closed)."""
    return names.name(pid) in LINUX_SHELL_SURFACE_PROCESSES


def _outer_rect(display: X11Display, window: int) -> Rect | None:
    attrs = display.attributes(window)
    position = display.root_position(window)
    if attrs is None or position is None:
        return None
    x, y = position
    width, height = attrs.width, attrs.height
    frame = display.cardinals(window, "_NET_FRAME_EXTENTS")
    if frame is not None and len(frame) >= 4:
        left, right, top, bottom = (_signed(value) for value in frame[:4])
        x, y = x - left, y - top
        width, height = width + left + right, height + top + bottom
    shadow = display.cardinals(window, "_GTK_FRAME_EXTENTS")
    if shadow is not None and len(shadow) >= 4:
        left, right, top, bottom = (_signed(value) for value in shadow[:4])
        if left + right < width and top + bottom < height:
            x, y = x + left, y + top
            width, height = width - left - right, height - top - bottom
    return Rect(x, y, width, height)


def _signed(value: int) -> int:
    """A 32-bit CARDINAL as a non-negative extent (garbage counts as 0)."""
    return value if 0 <= value < 0x10000 else 0


def _title(display: X11Display, window: int) -> str:
    title = display.utf8_text(window, "_NET_WM_NAME")
    if title is None:
        title = display.legacy_text(window, "WM_NAME")
    return title or ""


class _X11Inventory:
    """``WindowInventoryBackend`` for any EWMH window manager on Xorg."""

    def list_windows(self) -> tuple[DesktopInfo, list[DesktopWindow]]:
        with open_display() as display:
            display.require_ewmh()
            return self._list(display)

    def _list(self, display: X11Display) -> tuple[DesktopInfo, list[DesktopWindow]]:
        stacking = display.cardinals(display.root, "_NET_CLIENT_LIST_STACKING")
        if stacking is None:
            raise CaptureUnavailableError(
                CaptureStatus.COMPOSITOR_UNSUPPORTED, EWMH_DETAIL
            )
        desktops = _desktops(display)
        active = display.cardinal(display.root, "_NET_ACTIVE_WINDOW")
        names = ProcessNames()
        current_windows: list[DesktopWindow] = []
        other_windows: list[DesktopWindow] = []
        # Topmost first.
        for window in reversed(stacking):
            entry = self._window(display, window, desktops, active, names)
            if entry is None:
                continue
            if entry.desktop.current:
                current_windows.append(entry)
            elif entry.capturable:
                other_windows.append(entry)
        current_windows = _with_overlays(display, stacking, current_windows, names)
        ordered = [
            _with_z(entry, z)
            for z, entry in enumerate([*current_windows, *other_windows])
        ]
        info = DesktopInfo(
            current=None if desktops.current is None else desktops.current + 1,
            count=desktops.count,
            names=desktops.names,
        )
        return info, ordered

    def _window(
        self,
        display: X11Display,
        window: int,
        desktops: _Desktops,
        active: int | None,
        names: ProcessNames,
    ) -> DesktopWindow | None:
        if window == 0:
            return None
        kind = _window_kind(display, window)
        pid = display.cardinal(window, "_NET_WM_PID") or None
        if kind == "shell":
            if _is_shell_surface(pid, names):
                return None
            kind = "overlay"
        rect = _outer_rect(display, window)
        if rect is None or rect.is_empty:
            # Gone since the list was read, or zero-size.
            return None
        states = display.atom_names_present(window, "_NET_WM_STATE", _STATE_ATOMS)
        return DesktopWindow(
            window_id=str(window),
            pid=pid,
            process=names.name(pid),
            title=_title(display, window) if kind == "app" else "",
            rect=rect,
            minimized="_NET_WM_STATE_HIDDEN" in states,
            focused=active is not None and window == active,
            z=0,
            desktop=_window_desktop(
                display.cardinal(window, "_NET_WM_DESKTOP"),
                "_NET_WM_STATE_STICKY" in states,
                desktops,
            ),
            # A pid-less window's WM_CLASS is chosen by the client itself.
            capturable=kind == "app" and pid is not None,
        )


def _with_overlays(
    display: X11Display,
    stacking: list[int],
    current_windows: list[DesktopWindow],
    names: ProcessNames,
) -> list[DesktopWindow]:
    """``current_windows`` (topmost first) with every mapped override-redirect
    window merged in by the root window's stacking order, also on a desktop
    with no app window and below the lowest app window: one below the apps
    gets a higher ``z``, so the obscured check ignores it, but the screen rule
    still sees it. Fails closed (``capture_failed``) when the tree cannot be
    read: the obscured check and the screen rule need every window."""
    tree = display.root_children()
    if tree is None:
        raise CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, _TREE_DETAIL)
    position = {xid: index for index, xid in enumerate(tree)}
    frames: set[int] = set()
    frame_of: dict[int, int | None] = {}
    for client in stacking:
        frame = display.top_level(client) if client else None
        frame_of[client] = frame
        frames.add(frame if frame is not None else client)
    # Tree position of each managed current window, strictly decreasing in
    # the EWMH order (a window missing from the tree sits just below the one
    # above it).
    keyed: list[tuple[float, DesktopWindow]] = []
    above = float(len(tree))
    for entry in current_windows:
        frame = frame_of.get(int(entry.window_id))
        found = position.get(frame) if frame is not None else None
        key = float(found) if found is not None and found < above else above - 0.5
        keyed.append((key, entry))
        above = key
    screen = display.root_rect()
    for index, xid in enumerate(tree):
        if xid in frames:
            continue
        overlay = _override_redirect_window(display, xid, screen, names)
        if overlay is not None:
            keyed.append((float(index), overlay))
    keyed.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in keyed]


def _window_manager_pid(display: X11Display) -> int | None:
    check = display.cardinal(display.root, "_NET_SUPPORTING_WM_CHECK")
    if not check:
        return None
    return display.cardinal(check, "_NET_WM_PID") or None


def shell_draws_overlays(display: X11Display) -> bool:
    """Whether the window manager draws other programs' notifications and
    dialogs in its compositor stage, where no X window shows them (module
    docstring): its process (``_NET_SUPPORTING_WM_CHECK`` ->
    ``_NET_WM_PID``) is a known such shell, or the check window's
    ``_NET_WM_NAME`` names one."""
    pid = _window_manager_pid(display)
    if pid is not None and ProcessNames().name(pid) in _STAGE_OVERLAY_SHELLS:
        return True
    check = display.cardinal(display.root, "_NET_SUPPORTING_WM_CHECK")
    if not check:
        return False
    name = (display.utf8_text(check, "_NET_WM_NAME") or "").lower()
    return any(part in name for part in _STAGE_OVERLAY_WM_NAMES)


def _override_redirect_window(
    display: X11Display,
    xid: int,
    screen: Rect,
    names: ProcessNames,
) -> DesktopWindow | None:
    """A mapped, drawn override-redirect root child as a non-capturable
    overlay; None for anything else (managed frames, unmapped or input-only
    windows, zero-size or off-screen windows, and dock or desktop windows of
    a known shell process).

    The window manager's own override-redirect windows are overlays too:
    KWin's and xfwm4's Alt+Tab switchers and on-screen displays show other
    programs' titles and thumbnails. No window manager window is exempt."""
    attrs = display.attributes(xid)
    if (
        attrs is None
        or not attrs.override_redirect
        or not attrs.viewable
        or attrs.input_only
    ):
        return None
    width = attrs.width + 2 * attrs.border_width
    height = attrs.height + 2 * attrs.border_width
    rect = Rect(attrs.x, attrs.y, width, height)
    if rect.is_empty or not rect.intersects(screen):
        return None
    pid = display.cardinal(xid, "_NET_WM_PID") or None
    if _window_kind(display, xid) == "shell" and _is_shell_surface(pid, names):
        return None
    return DesktopWindow(
        window_id=str(xid),
        pid=pid,
        process=names.name(pid),
        title="",
        rect=rect,
        minimized=False,
        focused=False,
        z=0,
        desktop=WindowDesktop(current=True, index=None, name=None),
        capturable=False,
    )


def _window_desktop(
    raw: int | None, sticky: bool, desktops: _Desktops
) -> WindowDesktop:
    if sticky or raw == _ALL_DESKTOPS:
        return WindowDesktop(current=True, index=None, name=None)
    if raw is None or desktops.current is None:
        # No desktop information (single-desktop managers): treat as shown.
        return WindowDesktop(
            current=True, index=None if raw is None else raw + 1, name=None
        )
    if desktops.count is not None and raw >= desktops.count:
        return WindowDesktop(current=False, index=None, name=None)
    name = desktops.names[raw] if raw < len(desktops.names) else ""
    return WindowDesktop(
        current=raw == desktops.current, index=raw + 1, name=name or None
    )


def _with_z(entry: DesktopWindow, z: int) -> DesktopWindow:
    return replace(entry, z=z)


INVENTORY: WindowInventoryBackend = _X11Inventory()
