"""Read-only Win32 window enumeration, and the capture inventory on Windows.

Two users:

* ``app_closer_windows`` (close_app / close_tab) imports the primitives
  (``top_level_hwnds``, ``is_app_frame``, ``is_cloaked``, ``window_title``,
  ``window_pid``, ``process_exe``) and keeps its own rule: every cloaked
  window is left out.
* The window capture imports ``INVENTORY``. App windows (visible, unowned,
  not tool windows, titled) are ``capturable``; every other visible window
  of the current desktop (tool, owned and untitled windows: popups, toasts,
  overlays) is listed with ``capturable=False`` for the obscured check and
  the screen rule. Left out: the shell's taskbars, wallpaper and desktop
  (``Shell_TrayWnd``, ``Shell_SecondaryTrayWnd``, ``Progman``, ``WorkerW``),
  layered windows made fully transparent, windows cloaked by their app
  (``DWM_CLOAKED_APP``: a suspended UWP app) and ghosts cloaked on the
  current desktop. App windows cloaked by the shell (``DWM_CLOAKED_SHELL``:
  on another virtual desktop) are kept. It adds the minimised state, the
  foreground window, the frame rect and the z-order (``EnumWindows`` order).

Coordinates: every rect is read under a per-monitor DPI awareness context
(``per_monitor_dpi``), so it is in physical virtual-screen pixels, the space
Pillow's ``ImageGrab.grab(all_screens=True)`` uses (it switches its own
thread to per-monitor awareness for the grab). The process itself stays
system-DPI-aware (pyautogui), which would scale the rects of windows on a
monitor with another DPI. The rect is the visible frame
(``DWMWA_EXTENDED_FRAME_BOUNDS``, without the invisible resize borders),
``GetWindowRect`` when DWM does not answer.

Windows only: the implementation lives under ``sys.platform == "win32"``; on
other systems ``INVENTORY`` refuses with ``unsupported``.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

import psutil

from controller_client.capture_platform import WindowInventoryBackend
from controller_client.protocol import CaptureStatus, normalize_process_name
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopInfo,
    DesktopWindow,
    Rect,
    WindowDesktop,
)

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    from controller_client import win32_virtual_desktops

    # Moved verbatim from app_closer_windows.
    GW_OWNER: Final[int] = 4
    GWL_EXSTYLE: Final[int] = -20
    WS_EX_TOOLWINDOW: Final[int] = 0x00000080
    _WS_EX_LAYERED: Final[int] = 0x00080000
    _LWA_ALPHA: Final[int] = 0x2
    DWMWA_CLOAKED: Final[int] = 14
    TITLE_MAX_CHARS: Final[int] = 512

    DWMWA_EXTENDED_FRAME_BOUNDS: Final[int] = 9
    DWM_CLOAKED_APP: Final[int] = 0x1
    GA_ROOT: Final[int] = 2
    GA_ROOTOWNER: Final[int] = 3
    _CLASS_NAME_MAX_CHARS: Final[int] = 256
    _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE: Final[int] = -3
    _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: Final[int] = -4
    # The wallpaper / desktop icons and the taskbars: shown, unowned and not
    # tool windows, but not app windows.
    _SHELL_CLASSES: Final[frozenset[str]] = frozenset(
        {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"}
    )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetLayeredWindowAttributes.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetLayeredWindowAttributes.restype = wintypes.BOOL
    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    try:
        # Windows 10 1607 and later; DPI_AWARENESS_CONTEXT is a handle.
        _SetThreadDpiAwarenessContext = user32.SetThreadDpiAwarenessContext
        _SetThreadDpiAwarenessContext.argtypes = [ctypes.c_ssize_t]
        _SetThreadDpiAwarenessContext.restype = ctypes.c_ssize_t
        _HAS_DPI_CONTEXT = True
    except AttributeError:
        _HAS_DPI_CONTEXT = False

    @contextmanager
    def per_monitor_dpi() -> Iterator[None]:
        """Run the block with this thread per-monitor DPI aware, so rects and
        points are physical pixels (see the module docstring); restores the
        thread's previous context. A no-op before Windows 10 1607."""
        if not _HAS_DPI_CONTEXT:
            yield
            return
        previous = int(
            _SetThreadDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        )
        if not previous:
            # Before Windows 10 1703 (no v2).
            previous = int(
                _SetThreadDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE)
            )
        try:
            yield
        finally:
            if previous:
                _SetThreadDpiAwarenessContext(previous)

    # --- Primitives moved verbatim from app_closer_windows ---------------

    def window_title(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(TITLE_MAX_CHARS)
        user32.GetWindowTextW(hwnd, buffer, TITLE_MAX_CHARS)
        return buffer.value

    def window_pid(hwnd: int) -> int:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def is_cloaked(hwnd: int) -> bool:
        # Suspended UWP apps keep visible-but-cloaked windows nobody can see.
        cloaked = wintypes.DWORD(0)
        result = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return result == 0 and cloaked.value != 0

    def is_app_frame(hwnd: int) -> bool:
        """Visible, unowned and not a tool window (cloaked or not)."""
        if not user32.IsWindowVisible(hwnd):
            return False
        if user32.GetWindow(hwnd, GW_OWNER):
            return False
        return not user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW

    def process_exe(pid: int, cache: dict[int, tuple[str, str]]) -> tuple[str, str]:
        cached = cache.get(pid)
        if cached is not None:
            return cached
        exe_path = ""
        exe_name = ""
        try:
            process = psutil.Process(pid)
            exe_name = process.name().lower()
            try:
                exe_path = process.exe()
            except psutil.AccessDenied:
                # Elevated processes still expose their name.
                exe_path = process.name()
        except psutil.Error:
            pass
        cache[pid] = (exe_path, exe_name)
        return exe_path, exe_name

    def top_level_hwnds() -> list[int]:
        """Every top-level window, topmost first (``EnumWindows`` order)."""
        hwnds: list[int] = []

        def collect(hwnd: int | None, _lparam: int) -> bool:
            if hwnd:
                hwnds.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(collect), 0)
        return hwnds

    # --- Read-only additions for the capture ------------------------------

    def cloak_flags(hwnd: int) -> int:
        """The ``DWM_CLOAKED_*`` bits of ``hwnd`` (0: not cloaked or unknown)."""
        cloaked = wintypes.DWORD(0)
        result = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return int(cloaked.value) if result == 0 else 0

    def class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(_CLASS_NAME_MAX_CHARS)
        user32.GetClassNameW(hwnd, buffer, _CLASS_NAME_MAX_CHARS)
        return buffer.value

    def root_window(hwnd: int) -> int:
        """The top-level window ``hwnd`` belongs to (``GA_ROOT``)."""
        root = user32.GetAncestor(hwnd, GA_ROOT)
        return int(root) if root else hwnd

    def _rect(value: wintypes.RECT) -> Rect:
        return Rect(
            int(value.left),
            int(value.top),
            int(value.right - value.left),
            int(value.bottom - value.top),
        )

    def outer_window_rect(hwnd: int) -> Rect | None:
        """``GetWindowRect`` (with the invisible resize borders); call it under
        ``per_monitor_dpi``."""
        value = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(value)):
            return None
        return _rect(value)

    def window_rect(hwnd: int) -> Rect | None:
        """The visible frame (``DWMWA_EXTENDED_FRAME_BOUNDS``, physical
        pixels), else ``GetWindowRect``; call it under ``per_monitor_dpi``."""
        value = wintypes.RECT()
        result = dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if result == 0:
            rect = _rect(value)
            if not rect.is_empty:
                return rect
        return outer_window_rect(hwnd)

    def _fully_transparent(hwnd: int) -> bool:
        """A layered window whose constant alpha is 0 (drawn nowhere). A
        per-pixel alpha window (``UpdateLayeredWindow``) gives no answer and
        counts as drawn."""
        if not user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & _WS_EX_LAYERED:
            return False
        key = wintypes.DWORD(0)
        alpha = ctypes.c_ubyte(255)
        flags = wintypes.DWORD(0)
        if not user32.GetLayeredWindowAttributes(
            hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)
        ):
            return False
        return bool(flags.value & _LWA_ALPHA) and alpha.value == 0

    def _focused_roots() -> frozenset[int]:
        # The foreground window may be an owned dialog of the listed window.
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return frozenset()
        owner = user32.GetAncestor(hwnd, GA_ROOTOWNER)
        return frozenset({int(hwnd), int(owner) if owner else int(hwnd)})

    @dataclass(frozen=True)
    class _Candidate:
        hwnd: int
        pid: int
        process: str
        title: str
        rect: Rect
        minimized: bool
        focused: bool
        on_current: bool
        desktop_id: uuid.UUID | None
        capturable: bool

    def _candidates(
        manager: win32_virtual_desktops.VirtualDesktopManager | None,
    ) -> list[_Candidate]:
        focused = _focused_roots()
        cache: dict[int, tuple[str, str]] = {}
        found: list[_Candidate] = []
        for hwnd in top_level_hwnds():
            if not user32.IsWindowVisible(hwnd):
                continue
            if class_name(hwnd).lower() in _SHELL_CLASSES:
                continue
            flags = cloak_flags(hwnd)
            if flags & DWM_CLOAKED_APP:
                continue
            rect = window_rect(hwnd)
            if rect is None or rect.is_empty:
                continue
            title = window_title(hwnd)
            capturable = bool(title) and is_app_frame(hwnd)
            if not capturable and _fully_transparent(hwnd):
                continue
            on_current = manager.is_on_current(hwnd) if manager is not None else None
            if on_current is None:
                # No desktop answer: only what DWM draws counts as shown.
                on_current = flags == 0
            if flags and on_current:
                # Cloaked on the current desktop: drawn nowhere.
                continue
            if not capturable and not on_current:
                # Overlays matter only where they are drawn.
                continue
            desktop_id = (
                manager.desktop_id(hwnd) if manager is not None and capturable else None
            )
            if capturable and not on_current and desktop_id is None:
                # Cloaked by the shell, but on no desktop Windows knows.
                continue
            pid = window_pid(hwnd)
            _exe_path, exe_name = process_exe(pid, cache)
            found.append(
                _Candidate(
                    hwnd=hwnd,
                    pid=pid,
                    process=normalize_process_name(exe_name) or "",
                    title=title if capturable else "",
                    rect=rect,
                    minimized=bool(user32.IsIconic(hwnd)),
                    focused=hwnd in focused,
                    on_current=on_current,
                    desktop_id=desktop_id,
                    capturable=capturable,
                )
            )
        return found

    def _current_desktop(
        candidates: list[_Candidate], order: tuple[uuid.UUID, ...]
    ) -> uuid.UUID | None:
        # The desktop most uncloaked current windows report (a window pinned
        # to all desktops may report another one), else the registry value.
        votes: dict[uuid.UUID, int] = {}
        for candidate in candidates:
            desktop = candidate.desktop_id
            if candidate.on_current and desktop is not None and desktop in order:
                votes[desktop] = votes.get(desktop, 0) + 1
        if votes:
            return max(votes, key=lambda desktop: votes[desktop])
        current = win32_virtual_desktops.registry_current_desktop()
        return current if current is not None and current in order else None

    class _Win32Inventory:
        def list_windows(self) -> tuple[DesktopInfo, list[DesktopWindow]]:
            order = win32_virtual_desktops.desktop_order()
            names = win32_virtual_desktops.desktop_names(order) if order else ()
            with win32_virtual_desktops.open_manager() as manager, per_monitor_dpi():
                candidates = _candidates(manager)
            current_index: int | None = None
            if order:
                current = _current_desktop(candidates, order)
                if current is not None:
                    current_index = order.index(current) + 1

            def desktop_of(candidate: _Candidate) -> WindowDesktop:
                if candidate.on_current:
                    index = current_index
                elif order and candidate.desktop_id in order:
                    index = order.index(candidate.desktop_id) + 1
                else:
                    index = None
                name = names[index - 1] if index is not None and names else ""
                return WindowDesktop(
                    current=candidate.on_current, index=index, name=name or None
                )

            # The current desktop's windows first, each group in z-order.
            ordered = [c for c in candidates if c.on_current] + [
                c for c in candidates if not c.on_current
            ]
            windows = [
                DesktopWindow(
                    window_id=str(candidate.hwnd),
                    pid=candidate.pid or None,
                    process=candidate.process,
                    title=candidate.title,
                    rect=candidate.rect,
                    minimized=candidate.minimized,
                    focused=candidate.focused,
                    z=z,
                    desktop=desktop_of(candidate),
                    capturable=candidate.capturable and candidate.pid != 0,
                )
                for z, candidate in enumerate(ordered)
            ]
            info = DesktopInfo(
                current=current_index,
                count=len(order) if order else None,
                names=names,
            )
            return info, windows

    INVENTORY: WindowInventoryBackend = _Win32Inventory()

else:

    class _UnsupportedInventory:
        def list_windows(self) -> tuple[DesktopInfo, list[DesktopWindow]]:
            raise CaptureUnavailableError(
                CaptureStatus.UNSUPPORTED, "this inventory needs Windows"
            )

    INVENTORY: WindowInventoryBackend = _UnsupportedInventory()
