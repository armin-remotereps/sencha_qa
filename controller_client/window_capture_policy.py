"""Which windows a capture may list or take, and which window it takes.

The selection, region, screen and obscured rules are pure functions over the
inventory (``window_model``). ``build_deny_rules`` is the one impure part: it reads the
runner's own process tree (``app_close_policy.current_runner_processes``)
and, on macOS, the bundle ids of the listed windows' apps.

Allowlist: the server's normalized process names. Deny list (applies even to
an allowlisted name):

* the runner's own process and its helpers (the person window and its
  children);
* the OS shell (``explorer``, Finder, Dock, gnome-shell, ...).

Terminals are not denied: a project that allowlists a terminal may capture
any of its windows, the one that started the runner included.

A window whose process is unknown ("") never matches the allowlist, and only
``capturable`` windows (normal app windows with a known pid) are ever
selected or listed. The obscured check and the screen rule look at every
listed window, capturable or not, except windows of allowlisted, non-denied
processes with a known pid (``obscuring_windows``): their pixels may be in a
shot anyway.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from controller_client.app_close_policy import current_runner_processes
from controller_client.exceptions import ExecutionError
from controller_client.protocol import (
    CaptureRegion,
    CaptureStatus,
    normalize_process_name,
)
from controller_client.shell_processes import (
    MACOS_OS_SHELL_BUNDLE_IDS,
    WINDOWS_OS_SHELL_EXES,
)
from controller_client.window_model import CaptureUnavailableError, DesktopWindow, Rect

if sys.platform == "win32":
    _PLATFORM_DENIED_RAW: Final[frozenset[str]] = frozenset(WINDOWS_OS_SHELL_EXES)
elif sys.platform == "darwin":
    import AppKit

    _DENIED_BUNDLE_IDS: Final[frozenset[str]] = frozenset(MACOS_OS_SHELL_BUNDLE_IDS)
    # Owner names of the same apps, for a pid whose bundle id cannot be read.
    _PLATFORM_DENIED_RAW: Final[frozenset[str]] = frozenset(
        {
            "Finder",
            "Dock",
            "loginwindow",
            "SystemUIServer",
            "WindowManager",
            "Control Center",
        }
    )
else:
    _PLATFORM_DENIED_RAW: Final[frozenset[str]] = frozenset(
        {
            # OS shells and desktop components.
            "gnome-shell",
            "plasmashell",
            "kwin_x11",
            "kwin_wayland",
            "xfdesktop",
            "xfce4-panel",
            "mate-panel",
            "cinnamon",
            "budgie-panel",
            "lxpanel",
            "lxqt-panel",
            "waybar",
            "swaybar",
            "xdg-desktop-portal",
            "xdg-desktop-portal-gnome",
            "xdg-desktop-portal-gtk",
            "xdg-desktop-portal-kde",
        }
    )


def _normalized(names: Iterable[str]) -> frozenset[str]:
    return frozenset(
        name for name in (normalize_process_name(raw) for raw in names) if name
    )


# Normalized names denied on this platform (OS shells).
DENIED_PROCESS_NAMES: Final[frozenset[str]] = _normalized(_PLATFORM_DENIED_RAW)

_RUNNER_UNKNOWN_DETAIL: Final[str] = (
    "the Test Runner cannot identify its own processes, so nothing is captured"
)
_NOT_ALLOWED_DETAIL: Final[str] = "the matching window is not an allowed application"
_NOT_FOUND_DETAIL: Final[str] = "no window matches"
_MINIMIZED_DETAIL: Final[str] = "the matching window is minimised"
_OTHER_DESKTOP_DETAIL: Final[str] = "the matching window is on another desktop"
_UNKNOWN_PROCESS_LABEL: Final[str] = "unknown"


@dataclass(frozen=True)
class DenyRules:
    """Windows never listed or captured: by owning pid, or by process name.

    ``runner_pids`` (the runner itself and its helpers, the person window
    among them) is a subset of ``pids``: their windows are hidden for a
    capture, so they are also kept away from the desktop switcher.
    """

    pids: frozenset[int]
    processes: frozenset[str]
    runner_pids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class WindowSelection:
    """The window a capture takes."""

    window: DesktopWindow
    # Further allowlisted windows that matched too.
    other_matches: int
    # On another desktop: the service switches there first.
    needs_switch: bool


class SelectionRefusedError(CaptureUnavailableError):
    """No window can be taken; ``window`` is the refused match when the reply
    may name it (minimised, on another desktop), else None."""

    def __init__(
        self,
        status: CaptureStatus,
        detail: str,
        window: DesktopWindow | None = None,
        other_matches: int = 0,
    ) -> None:
        super().__init__(status, detail)
        self.window = window
        self.other_matches = other_matches


if sys.platform == "darwin":

    def _denied_bundle_pids(windows: Sequence[DesktopWindow]) -> frozenset[int]:
        """Pids of the listed windows whose app is an OS shell
        (app_closer_macos's bundle id list)."""
        denied: set[int] = set()
        for pid in {window.pid for window in windows if window.pid is not None}:
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
                pid
            )
            bundle_id = None if app is None else app.bundleIdentifier()
            if bundle_id is not None and bundle_id in _DENIED_BUNDLE_IDS:
                denied.add(pid)
        return frozenset(denied)

else:

    def _denied_bundle_pids(windows: Sequence[DesktopWindow]) -> frozenset[int]:
        return frozenset()


def build_deny_rules(windows: Sequence[DesktopWindow]) -> DenyRules:
    """The deny list for one inventory (blocking; worker thread).

    Fails closed: raises ``CaptureUnavailableError(capture_failed)`` when the
    runner's own processes cannot be read.
    """
    try:
        runner = current_runner_processes()
    except ExecutionError as e:
        raise CaptureUnavailableError(
            CaptureStatus.CAPTURE_FAILED, _RUNNER_UNKNOWN_DETAIL
        ) from e
    runner_pids = frozenset({runner.own_pid, *runner.helper_pids})
    pids = set(runner_pids)
    pids.update(_denied_bundle_pids(windows))
    return DenyRules(
        pids=frozenset(pids),
        processes=DENIED_PROCESS_NAMES,
        runner_pids=runner_pids,
    )


def is_denied(window: DesktopWindow, deny: DenyRules) -> bool:
    return (
        not window.process
        or window.process in deny.processes
        or (window.pid is not None and window.pid in deny.pids)
    )


def is_allowed(window: DesktopWindow, allowed: frozenset[str], deny: DenyRules) -> bool:
    """Capturable, allowlisted, not denied, and not hidden by the app or OS
    itself."""
    return (
        window.capturable
        and window.pid is not None
        and window.process in allowed
        and not window.app_cloaked
        and not is_denied(window, deny)
    )


def listable_windows(
    windows: Sequence[DesktopWindow], allowed: frozenset[str], deny: DenyRules
) -> list[DesktopWindow]:
    """The windows ``list_windows`` reports (every desktop, topmost first)."""
    return sorted(
        (window for window in windows if is_allowed(window, allowed, deny)),
        key=lambda window: window.z,
    )


def capturable_count(windows: Sequence[DesktopWindow]) -> int:
    """How many app windows the inventory holds (overlays not counted)."""
    return sum(1 for window in windows if window.capturable)


def switcher_windows(
    windows: Sequence[DesktopWindow], deny: DenyRules
) -> list[DesktopWindow]:
    """The windows a desktop switcher may record or anchor on: app windows
    with a known pid, never the runner's own (the person window is hidden
    for the capture, so it cannot prove a desktop is shown, and it keeps no
    workspace alive)."""
    return [
        window
        for window in windows
        if window.capturable
        and window.pid is not None
        and window.pid not in deny.runner_pids
    ]


def _matches(
    window: DesktopWindow, process: str | None, title_contains: str | None
) -> bool:
    if not window.capturable or window.app_cloaked:
        return False
    if window.rect.is_empty and not window.minimized:
        return False
    if process is not None and window.process != process:
        return False
    if title_contains is not None:
        return title_contains.casefold() in window.title.casefold()
    return True


def select_window(
    windows: Sequence[DesktopWindow],
    *,
    window_id: str | None,
    process: str | None,
    title_contains: str | None,
    allowed: frozenset[str],
    deny: DenyRules,
    switch_desktop: bool,
) -> WindowSelection:
    """The window a capture takes, or ``SelectionRefusedError``.

    An explicit ``window_id`` wins. Otherwise the allowlisted, non-denied
    windows matching ``process`` and ``title_contains`` (case-insensitive;
    no criterion: every allowlisted window) are ranked: the current desktop
    first, then the topmost non-minimised one. ``not_allowed`` when only
    non-allowlisted or denied windows matched, ``window_not_found`` when none
    did, ``window_minimized`` when every allowlisted match is minimised. A
    match only on another desktop is selected with ``needs_switch``, or
    refused with ``window_on_other_desktop`` when ``switch_desktop`` is
    False.
    """
    if window_id is not None:
        found = next((w for w in windows if w.window_id == window_id), None)
        if found is None:
            raise SelectionRefusedError(
                CaptureStatus.WINDOW_NOT_FOUND, _NOT_FOUND_DETAIL
            )
        if not is_allowed(found, allowed, deny):
            raise SelectionRefusedError(CaptureStatus.NOT_ALLOWED, _NOT_ALLOWED_DETAIL)
        return _rank([found], switch_desktop=switch_desktop)
    matching = [w for w in windows if _matches(w, process, title_contains)]
    allowed_matches = [w for w in matching if is_allowed(w, allowed, deny)]
    if not allowed_matches:
        if matching:
            raise SelectionRefusedError(CaptureStatus.NOT_ALLOWED, _NOT_ALLOWED_DETAIL)
        raise SelectionRefusedError(CaptureStatus.WINDOW_NOT_FOUND, _NOT_FOUND_DETAIL)
    return _rank(allowed_matches, switch_desktop=switch_desktop)


def _rank(
    candidates: Sequence[DesktopWindow], *, switch_desktop: bool
) -> WindowSelection:
    ordered = sorted(candidates, key=lambda window: window.z)
    other_matches = len(ordered) - 1
    shown = [w for w in ordered if not w.minimized]
    if not shown:
        raise SelectionRefusedError(
            CaptureStatus.WINDOW_MINIMIZED,
            _MINIMIZED_DETAIL,
            window=ordered[0],
            other_matches=other_matches,
        )
    current = [w for w in shown if w.desktop.current]
    if current:
        return WindowSelection(
            window=current[0], other_matches=other_matches, needs_switch=False
        )
    if not switch_desktop:
        raise SelectionRefusedError(
            CaptureStatus.WINDOW_ON_OTHER_DESKTOP,
            _OTHER_DESKTOP_DETAIL,
            window=shown[0],
            other_matches=other_matches,
        )
    return WindowSelection(
        window=shown[0], other_matches=other_matches, needs_switch=True
    )


def check_region(region: CaptureRegion, width: int, height: int) -> None:
    """Refuse a region that does not lie inside a ``width`` x ``height`` shot
    (``region_out_of_bounds`` with the shot size as ``"<w>x<h>"``)."""
    if region.x + region.w > width or region.y + region.h > height:
        raise CaptureUnavailableError(
            CaptureStatus.REGION_OUT_OF_BOUNDS, f"{width}x{height}"
        )


def _shown_on_current_desktop(window: DesktopWindow) -> bool:
    return (
        window.desktop.current
        and window.drawn
        and not window.minimized
        and not window.app_cloaked
        and not window.rect.is_empty
    )


def foreign_processes_on_monitor(
    monitor: Rect,
    windows: Sequence[DesktopWindow],
    allowed: frozenset[str],
    deny: DenyRules,
) -> list[str]:
    """The screen rule: the processes of every visible window on the current
    desktop (app windows and overlays alike) that overlaps ``monitor`` and is
    not of an allowlisted, non-denied process with a known pid (sorted,
    unique; "unknown" for an unknown process). A ``screen`` capture is
    refused with ``screen_not_clean`` when this is not empty."""
    foreign: set[str] = set()
    for window in windows:
        if not _shown_on_current_desktop(window):
            continue
        if not window.rect.intersects(monitor):
            continue
        if (
            window.pid is None
            or window.process not in allowed
            or is_denied(window, deny)
        ):
            foreign.add(window.process or _UNKNOWN_PROCESS_LABEL)
    return sorted(foreign)


def unobscured_by_stacking(
    window: DesktopWindow, windows: Sequence[DesktopWindow]
) -> bool:
    """Whether ``window`` is fully visible on the current desktop: no visible
    window stacked above it (lower ``z``) of another process overlaps any
    part of its rect. ``windows`` is the inventory ``window`` came from,
    with every visible window, overlays included. A window with an unknown
    pid is foreign to every other window. False for a minimised, undrawn or
    empty window, or one on another desktop.

    The one obscured rule of every platform (``WindowGrabBackend.
    is_unobscured``): rectangle intersection, never sample points, so a
    small popup, toast or terminal anywhere over the window counts.
    """
    if not _shown_on_current_desktop(window):
        return False
    for other in windows:
        if other.window_id == window.window_id or other.z >= window.z:
            continue
        if not _shown_on_current_desktop(other):
            continue
        if window.pid is not None and other.pid == window.pid:
            continue
        if other.rect.intersects(window.rect):
            return False
    return True


def obscuring_windows(
    window: DesktopWindow,
    windows: Sequence[DesktopWindow],
    allowed: frozenset[str],
    deny: DenyRules,
) -> list[DesktopWindow]:
    """``windows`` without the other windows whose pixels may be in a shot:
    those of an allowlisted, non-denied process with a known pid (the screen
    rule's test). ``window`` itself is kept. What the obscured check
    (``WindowGrabBackend.is_unobscured``) and ``covering_processes`` look
    at, so allowlisting an overlay's process lets a covered window be
    grabbed from the screen, as it lets the screen be grabbed."""
    return [
        other
        for other in windows
        if other.window_id == window.window_id
        or other.pid is None
        or other.process not in allowed
        or is_denied(other, deny)
    ]


def covering_processes(
    window: DesktopWindow, windows: Sequence[DesktopWindow]
) -> list[str]:
    """The processes of the visible windows stacked above ``window`` that
    overlap it (``unobscured_by_stacking``'s test; sorted, unique, "unknown"
    for an unknown process). Names only, never titles."""
    covering: set[str] = set()
    for other in windows:
        if other.window_id == window.window_id or other.z >= window.z:
            continue
        if not _shown_on_current_desktop(other):
            continue
        if window.pid is not None and other.pid == window.pid:
            continue
        if other.rect.intersects(window.rect):
            covering.add(other.process or _UNKNOWN_PROCESS_LABEL)
    return sorted(covering)
