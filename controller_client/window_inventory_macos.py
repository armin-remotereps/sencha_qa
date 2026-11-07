"""Window inventory for the window capture on macOS.

Exports ``INVENTORY`` (``capture_platform.WindowInventoryBackend``).

Windows come from the window server: ``CGWindowListCopyWindowInfo`` with
``kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements`` lists every
window, on any Space, front to back across all layers for the windows on
screen. Layer-0 windows are the app windows (``capturable``). Every other
on-screen window (floating panels, toasts, popup menus, overlays) is listed
with ``capturable=False`` for the obscured check and the screen rule, except
the OS shell's own chrome: the menu bar and its status items (main menu and
status layers, and the window server's own windows) and the Dock's
full-display window. A window on screen is on the current Space. A window that is not on
screen is minimised, belongs to a hidden app (Cmd+H), or is on another Space;
the window server does not say which, so:

* a hidden app (``NSRunningApplication.isHidden``) counts as minimised;
* with Accessibility, the app's ``AXWindows`` are matched by bounds and title
  (``match_ax_window``): ``AXMinimized`` true means minimised. A window not
  matched, or matched and not minimised, is on another Space (``AXWindows``
  usually lists only the current Space's and the minimised windows);
* without Accessibility it cannot be classified and reads "another Space";
  ``macos_desktop_switch`` then refuses the switch with a detail asking for
  Accessibility (which the switch needs anyway).

Space numbers and names need private APIs, so ``DesktopInfo`` and every
``WindowDesktop`` carry no index or name.

This module also holds the small ctypes binding of the Accessibility API
(``ApplicationServices``) that ``macos_desktop_switch`` shares: pyobjc's
``ApplicationServices`` framework package is not installed (only Quartz and
Cocoa come with pyautogui) and no pip dependency may be added.

Rects are global display points with a top-left origin (``kCGWindowBounds``),
the space ``screencapture -R`` takes. Logs carry no titles.
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    import ctypes
    import functools
    import logging
    import threading
    import weakref
    from collections.abc import Mapping, Sequence
    from dataclasses import dataclass
    from typing import Final

    import AppKit
    import Quartz

    from controller_client.capture_platform import WindowInventoryBackend
    from controller_client.protocol import normalize_process_name
    from controller_client.window_model import (
        DesktopInfo,
        DesktopWindow,
        Rect,
        WindowDesktop,
    )

    logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Accessibility (ctypes against ApplicationServices / CoreFoundation)
    # ------------------------------------------------------------------

    _APPLICATION_SERVICES_PATH: Final[str] = (
        "/System/Library/Frameworks/ApplicationServices.framework/"
        "ApplicationServices"
    )
    _CORE_FOUNDATION_PATH: Final[str] = (
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    _CF_STRING_ENCODING_UTF8: Final[int] = 0x08000100
    _AX_SUCCESS: Final[int] = 0
    _AX_VALUE_CGPOINT: Final[int] = 1
    _AX_VALUE_CGSIZE: Final[int] = 2
    # An app that does not answer within this long gives an AX error instead
    # of blocking the capture thread (the default is 6 s per call).
    _AX_MESSAGING_TIMEOUT_SECONDS: Final[float] = 1.0
    # Longest title read through AX (characters); longer ones read as "".
    _AX_TITLE_MAX_CHARS: Final[int] = 4096
    # Bounds of an AX window and its window-server window agree to a point;
    # allow for rounding.
    _AX_RECT_TOLERANCE: Final[int] = 2

    class _CGPointStruct(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class _CGSizeStruct(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    @dataclass(frozen=True)
    class _AxLibrary:
        ax: ctypes.CDLL
        cf: ctypes.CDLL
        cf_boolean_true: int
        string_type_id: int
        boolean_type_id: int
        array_type_id: int
        ax_value_type_id: int
        ax_element_type_id: int

    @functools.cache
    def _ax_library() -> _AxLibrary | None:
        """The loaded frameworks with typed signatures, or None if they cannot
        be loaded (then Accessibility reads as not granted)."""
        try:
            ax = ctypes.CDLL(_APPLICATION_SERVICES_PATH)
            cf = ctypes.CDLL(_CORE_FOUNDATION_PATH)
        except OSError:
            logger.warning("Could not load the Accessibility framework")
            return None
        void_p = ctypes.c_void_p
        cf.CFRelease.argtypes = [void_p]
        cf.CFRelease.restype = None
        cf.CFRetain.argtypes = [void_p]
        cf.CFRetain.restype = void_p
        cf.CFGetTypeID.argtypes = [void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        for name in (
            "CFStringGetTypeID",
            "CFBooleanGetTypeID",
            "CFArrayGetTypeID",
        ):
            getattr(cf, name).argtypes = []
            getattr(cf, name).restype = ctypes.c_ulong
        cf.CFStringCreateWithCString.argtypes = [
            void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        cf.CFStringCreateWithCString.restype = void_p
        cf.CFStringGetLength.argtypes = [void_p]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        cf.CFStringGetCString.argtypes = [
            void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [void_p]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFArrayGetCount.argtypes = [void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = void_p

        ax.AXIsProcessTrusted.argtypes = []
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        ax.AXUIElementGetTypeID.argtypes = []
        ax.AXUIElementGetTypeID.restype = ctypes.c_ulong
        ax.AXValueGetTypeID.argtypes = []
        ax.AXValueGetTypeID.restype = ctypes.c_ulong
        ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        ax.AXUIElementCreateApplication.restype = void_p
        ax.AXUIElementCreateSystemWide.argtypes = []
        ax.AXUIElementCreateSystemWide.restype = void_p
        ax.AXUIElementCopyAttributeValue.argtypes = [
            void_p,
            void_p,
            ctypes.POINTER(void_p),
        ]
        ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        ax.AXUIElementSetAttributeValue.argtypes = [void_p, void_p, void_p]
        ax.AXUIElementSetAttributeValue.restype = ctypes.c_int32
        ax.AXUIElementPerformAction.argtypes = [void_p, void_p]
        ax.AXUIElementPerformAction.restype = ctypes.c_int32
        ax.AXUIElementGetPid.argtypes = [void_p, ctypes.POINTER(ctypes.c_int)]
        ax.AXUIElementGetPid.restype = ctypes.c_int32
        ax.AXUIElementSetMessagingTimeout.argtypes = [void_p, ctypes.c_float]
        ax.AXUIElementSetMessagingTimeout.restype = ctypes.c_int32
        ax.AXValueGetValue.argtypes = [void_p, ctypes.c_uint32, void_p]
        ax.AXValueGetValue.restype = ctypes.c_bool

        true_ref = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
        if not true_ref:
            return None
        return _AxLibrary(
            ax=ax,
            cf=cf,
            cf_boolean_true=true_ref,
            string_type_id=int(cf.CFStringGetTypeID()),
            boolean_type_id=int(cf.CFBooleanGetTypeID()),
            array_type_id=int(cf.CFArrayGetTypeID()),
            ax_value_type_id=int(ax.AXValueGetTypeID()),
            ax_element_type_id=int(ax.AXUIElementGetTypeID()),
        )

    _NAME_LOCK = threading.Lock()
    # Attribute and action names as CFStrings, created once and kept for the
    # process lifetime (a handful of constants).
    _NAME_REFS: dict[str, int] = {}

    def _cf_name(lib: _AxLibrary, name: str) -> int | None:
        with _NAME_LOCK:
            ref = _NAME_REFS.get(name)
            if ref is None:
                created = lib.cf.CFStringCreateWithCString(
                    None, name.encode("ascii"), _CF_STRING_ENCODING_UTF8
                )
                if not created:
                    return None
                ref = int(created)
                _NAME_REFS[name] = ref
            return ref

    def _release(lib: _AxLibrary, ref: int) -> None:
        lib.cf.CFRelease(ref)

    class AxElement:
        """An owned ``AXUIElementRef``, released when this object is
        collected. Stays valid across Space switches (the switcher records
        one before switching and raises it on the way back)."""

        def __init__(
            self, lib: _AxLibrary, ref: int, *, system_wide: bool = False
        ) -> None:
            self._lib = lib
            self._ref = ref
            # On the system-wide element the timeout would become global.
            if not system_wide:
                lib.ax.AXUIElementSetMessagingTimeout(
                    ref, _AX_MESSAGING_TIMEOUT_SECONDS
                )
            weakref.finalize(self, _release, lib, ref)

        @property
        def ref(self) -> int:
            return self._ref

    @dataclass(frozen=True)
    class AxWindow:
        """One window of an app as Accessibility reports it."""

        element: AxElement
        title: str
        # Global points, top-left origin; None when position or size is
        # unreadable.
        rect: Rect | None
        minimized: bool

    def _copy_attribute(lib: _AxLibrary, element: int, name: str) -> int | None:
        """An owned CF reference to ``element``'s attribute, or None."""
        name_ref = _cf_name(lib, name)
        if name_ref is None:
            return None
        out = ctypes.c_void_p()
        error = lib.ax.AXUIElementCopyAttributeValue(
            element, name_ref, ctypes.byref(out)
        )
        if error != _AX_SUCCESS or not out.value:
            return None
        return int(out.value)

    def _read_string(lib: _AxLibrary, element: int, name: str) -> str:
        ref = _copy_attribute(lib, element, name)
        if ref is None:
            return ""
        try:
            if lib.cf.CFGetTypeID(ref) != lib.string_type_id:
                return ""
            length = int(lib.cf.CFStringGetLength(ref))
            if length <= 0 or length > _AX_TITLE_MAX_CHARS:
                return ""
            size = (
                int(
                    lib.cf.CFStringGetMaximumSizeForEncoding(
                        length, _CF_STRING_ENCODING_UTF8
                    )
                )
                + 1
            )
            buffer = ctypes.create_string_buffer(size)
            if not lib.cf.CFStringGetCString(
                ref, buffer, size, _CF_STRING_ENCODING_UTF8
            ):
                return ""
            return buffer.value.decode("utf-8", errors="replace")
        finally:
            _release(lib, ref)

    def _read_bool(lib: _AxLibrary, element: int, name: str) -> bool | None:
        ref = _copy_attribute(lib, element, name)
        if ref is None:
            return None
        try:
            if lib.cf.CFGetTypeID(ref) != lib.boolean_type_id:
                return None
            return bool(lib.cf.CFBooleanGetValue(ref))
        finally:
            _release(lib, ref)

    def _read_rect(lib: _AxLibrary, element: int) -> Rect | None:
        point = _CGPointStruct()
        size = _CGSizeStruct()
        for name, value_type, target in (
            ("AXPosition", _AX_VALUE_CGPOINT, ctypes.byref(point)),
            ("AXSize", _AX_VALUE_CGSIZE, ctypes.byref(size)),
        ):
            ref = _copy_attribute(lib, element, name)
            if ref is None:
                return None
            try:
                if lib.cf.CFGetTypeID(ref) != lib.ax_value_type_id:
                    return None
                if not lib.ax.AXValueGetValue(ref, value_type, target):
                    return None
            finally:
                _release(lib, ref)
        return Rect(
            round(point.x), round(point.y), round(size.width), round(size.height)
        )

    def ax_is_trusted() -> bool:
        """Whether this process may use Accessibility (``AXIsProcessTrusted``)."""
        lib = _ax_library()
        return lib is not None and bool(lib.ax.AXIsProcessTrusted())

    def _app_element(lib: _AxLibrary, pid: int) -> AxElement | None:
        ref = lib.ax.AXUIElementCreateApplication(pid)
        return AxElement(lib, int(ref)) if ref else None

    def ax_app_windows(pid: int) -> list[AxWindow]:
        """``pid``'s ``AXWindows`` (empty without Accessibility, or when the
        app does not answer)."""
        lib = _ax_library()
        if lib is None:
            return []
        app = _app_element(lib, pid)
        if app is None:
            return []
        array = _copy_attribute(lib, app.ref, "AXWindows")
        if array is None:
            return []
        windows: list[AxWindow] = []
        try:
            if lib.cf.CFGetTypeID(array) != lib.array_type_id:
                return []
            for index in range(int(lib.cf.CFArrayGetCount(array))):
                item = lib.cf.CFArrayGetValueAtIndex(array, index)
                if not item or lib.cf.CFGetTypeID(item) != lib.ax_element_type_id:
                    continue
                # The array owns its items: retain the one we keep.
                element = AxElement(lib, int(lib.cf.CFRetain(item)))
                windows.append(
                    AxWindow(
                        element=element,
                        title=_read_string(lib, element.ref, "AXTitle"),
                        rect=_read_rect(lib, element.ref),
                        minimized=bool(_read_bool(lib, element.ref, "AXMinimized")),
                    )
                )
        finally:
            _release(lib, array)
        return windows

    def ax_focused_app_pid() -> int | None:
        """The pid of the app holding the keyboard focus, through the
        system-wide element (current, unlike ``NSWorkspace`` without a run
        loop); None without Accessibility."""
        lib = _ax_library()
        if lib is None:
            return None
        system_ref = lib.ax.AXUIElementCreateSystemWide()
        if not system_ref:
            return None
        system = AxElement(lib, int(system_ref), system_wide=True)
        app_ref = _copy_attribute(lib, system.ref, "AXFocusedApplication")
        if app_ref is None:
            return None
        try:
            if lib.cf.CFGetTypeID(app_ref) != lib.ax_element_type_id:
                return None
            pid = ctypes.c_int()
            if lib.ax.AXUIElementGetPid(app_ref, ctypes.byref(pid)) != _AX_SUCCESS:
                return None
            return int(pid.value)
        finally:
            _release(lib, app_ref)

    def ax_raise(window: AxElement) -> bool:
        """``kAXRaiseAction`` on ``window``; whether the app accepted it."""
        lib = _ax_library()
        name = None if lib is None else _cf_name(lib, "AXRaise")
        if lib is None or name is None:
            return False
        return bool(lib.ax.AXUIElementPerformAction(window.ref, name) == _AX_SUCCESS)

    def ax_set_frontmost(pid: int) -> bool:
        """Set ``AXFrontmost`` of ``pid``'s app element to true (activation
        through Accessibility, which also works where macOS 14+ cooperative
        activation ignores ``activateWithOptions``)."""
        lib = _ax_library()
        if lib is None:
            return False
        app = _app_element(lib, pid)
        name = _cf_name(lib, "AXFrontmost")
        if app is None or name is None:
            return False
        error = lib.ax.AXUIElementSetAttributeValue(app.ref, name, lib.cf_boolean_true)
        return bool(error == _AX_SUCCESS)

    def _near(a: Rect, b: Rect) -> bool:
        return (
            abs(a.x - b.x) <= _AX_RECT_TOLERANCE
            and abs(a.y - b.y) <= _AX_RECT_TOLERANCE
            and abs(a.width - b.width) <= _AX_RECT_TOLERANCE
            and abs(a.height - b.height) <= _AX_RECT_TOLERANCE
        )

    def match_ax_window(
        rect: Rect, title: str, candidates: Sequence[AxWindow]
    ) -> AxWindow | None:
        """The AX window of the same app that is this window-server window:
        bounds within a point or two (an equal title breaks a tie), else the
        only one with the same non-empty title; None when unsure."""
        by_rect = [
            ax for ax in candidates if ax.rect is not None and _near(ax.rect, rect)
        ]
        if len(by_rect) == 1:
            return by_rect[0]
        if by_rect:
            titled = [ax for ax in by_rect if title and ax.title == title]
            return titled[0] if len(titled) == 1 else None
        if not title:
            return None
        titled = [ax for ax in candidates if ax.title == title]
        return titled[0] if len(titled) == 1 else None

    # ------------------------------------------------------------------
    # Window server
    # ------------------------------------------------------------------

    _NORMAL_WINDOW_LAYER: Final[int] = 0
    # kCGMainMenuWindowLevel and kCGStatusWindowLevel: the menu bar and its
    # status items (OS shell chrome, never an obstruction).
    _MENU_BAR_LAYERS: Final[frozenset[int]] = frozenset({24, 25})
    _WINDOW_SERVER_OWNER: Final[str] = "Window Server"
    _DOCK_OWNER: Final[str] = "Dock"
    _MAX_DISPLAYS: Final[int] = 32
    # A window not on screen is listed only with a title and at least this
    # size (points): apps keep untitled, tiny or never-shown helper windows
    # off screen, which are no capture target.
    _OFFSCREEN_MIN_SIDE: Final[int] = 40

    @dataclass(frozen=True)
    class CgWindow:
        """One window-server window (any layer)."""

        window_id: int
        pid: int
        owner: str
        title: str
        rect: Rect
        layer: int
        on_screen: bool
        alpha: float

    def _int(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    def _parse_bounds(raw: object) -> Rect | None:
        if not isinstance(raw, Mapping):
            return None
        numbers: list[int] = []
        for key in ("X", "Y", "Width", "Height"):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            numbers.append(round(float(value)))
        return Rect(numbers[0], numbers[1], numbers[2], numbers[3])

    def _parse_window(info: Mapping[str, object]) -> CgWindow | None:
        window_id = _int(info.get(Quartz.kCGWindowNumber))
        pid = _int(info.get(Quartz.kCGWindowOwnerPID))
        layer = _int(info.get(Quartz.kCGWindowLayer))
        rect = _parse_bounds(info.get(Quartz.kCGWindowBounds))
        if window_id is None or pid is None or layer is None or rect is None:
            return None
        owner = info.get(Quartz.kCGWindowOwnerName)
        title = info.get(Quartz.kCGWindowName)
        on_screen = info.get(Quartz.kCGWindowIsOnscreen)
        alpha = info.get(Quartz.kCGWindowAlpha)
        return CgWindow(
            window_id=window_id,
            pid=pid,
            owner=owner if isinstance(owner, str) else "",
            title=title if isinstance(title, str) else "",
            rect=rect,
            layer=layer,
            on_screen=bool(on_screen) if isinstance(on_screen, (bool, int)) else False,
            alpha=float(alpha) if isinstance(alpha, (int, float)) else 1.0,
        )

    def cg_windows(on_screen_only: bool) -> list[CgWindow]:
        """Every window-server window of any layer, front to back for those
        on screen (``kCGWindowListExcludeDesktopElements``)."""
        options = Quartz.kCGWindowListExcludeDesktopElements | (
            Quartz.kCGWindowListOptionOnScreenOnly
            if on_screen_only
            else Quartz.kCGWindowListOptionAll
        )
        raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        windows: list[CgWindow] = []
        for info in raw or ():
            parsed = _parse_window(info)
            if parsed is not None:
                windows.append(parsed)
        return windows

    def cg_window_on_screen(window_id: int) -> bool:
        """Whether window ``window_id`` is on screen now (on the current
        Space, not minimised, its app not hidden) and drawn (alpha > 0)."""
        raw = Quartz.CGWindowListCreateDescriptionFromArray([window_id])
        for info in raw or ():
            parsed = _parse_window(info)
            if parsed is not None and parsed.window_id == window_id:
                return parsed.on_screen and parsed.alpha > 0
        return False

    def display_rects() -> list[Rect]:
        """The active displays' bounds (global points, top-left origin)."""
        error, displays, count = Quartz.CGGetActiveDisplayList(
            _MAX_DISPLAYS, None, None
        )
        if error != 0 or not displays:
            return []
        rects: list[Rect] = []
        for display in list(displays)[:count]:
            bounds = Quartz.CGDisplayBounds(display)
            rects.append(
                Rect(
                    round(bounds.origin.x),
                    round(bounds.origin.y),
                    round(bounds.size.width),
                    round(bounds.size.height),
                )
            )
        return rects

    def _is_app_window(window: CgWindow) -> bool:
        if window.layer != _NORMAL_WINDOW_LAYER or window.rect.is_empty:
            return False
        if window.on_screen:
            return window.alpha > 0
        return (
            bool(window.title)
            and window.rect.width >= _OFFSCREEN_MIN_SIDE
            and window.rect.height >= _OFFSCREEN_MIN_SIDE
        )

    def _is_shell_chrome(window: CgWindow, displays: Sequence[Rect]) -> bool:
        """The menu bar, its status items, the window server's own windows,
        and the Dock's transparent full-display window (layer 20, spanning a
        display while the Dock shows; the Dock bar is drawn inside it)."""
        if window.owner == _WINDOW_SERVER_OWNER or window.layer in _MENU_BAR_LAYERS:
            return True
        return (
            window.owner == _DOCK_OWNER
            and window.layer > 0
            and any(window.rect == display for display in displays)
        )

    def _is_overlay(window: CgWindow, displays: Sequence[Rect]) -> bool:
        """A visible on-screen window that is not an app window and not shell
        chrome: listed as not capturable."""
        return (
            window.on_screen
            and window.layer != _NORMAL_WINDOW_LAYER
            and window.layer >= 0
            and window.alpha > 0
            and not window.rect.is_empty
            and not _is_shell_chrome(window, displays)
        )

    def running_app(pid: int) -> AppKit.NSRunningApplication | None:
        """``pid``'s running application, None when gone or terminated."""
        handle = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
            pid
        )
        if handle is None or handle.isTerminated():
            return None
        return handle

    _CURRENT: Final[WindowDesktop] = WindowDesktop(current=True, index=None, name=None)
    _OTHER: Final[WindowDesktop] = WindowDesktop(current=False, index=None, name=None)

    class _MacInventory:
        def list_windows(self) -> tuple[DesktopInfo, list[DesktopWindow]]:
            every = cg_windows(on_screen_only=False)
            displays = display_rects()
            candidates = [w for w in every if _is_app_window(w)]
            app_ids = {w.window_id for w in candidates}
            # Front to back across all layers: app windows and overlays.
            on_screen = [
                w
                for w in every
                if (w.window_id in app_ids and w.on_screen) or _is_overlay(w, displays)
            ]
            apps: dict[int, AppKit.NSRunningApplication | None] = {}

            def regular_app(pid: int) -> AppKit.NSRunningApplication | None:
                """``pid``'s app when it is a regular (Dock) app, else None."""
                if pid not in apps:
                    handle = running_app(pid)
                    regular = (
                        handle is not None
                        and handle.activationPolicy()
                        == AppKit.NSApplicationActivationPolicyRegular
                    )
                    apps[pid] = handle if regular else None
                return apps[pid]

            trusted = ax_is_trusted()
            front_pid = ax_focused_app_pid() if trusted else None
            if front_pid is None:
                # Without Accessibility: the topmost regular app's window
                # (Stage Manager and other agents draw layer-0 windows too).
                front_pid = next(
                    (
                        w.pid
                        for w in on_screen
                        if w.window_id in app_ids and regular_app(w.pid) is not None
                    ),
                    None,
                )

            ax_windows: dict[int, list[AxWindow]] = {}
            off_screen: list[tuple[CgWindow, bool]] = []
            for window in candidates:
                if window.on_screen:
                    continue
                app = regular_app(window.pid)
                if app is None:
                    # Agent / accessory apps cannot be activated: not a target.
                    continue
                minimized = bool(app.isHidden())
                if not minimized and trusted:
                    if window.pid not in ax_windows:
                        ax_windows[window.pid] = ax_app_windows(window.pid)
                    match = match_ax_window(
                        window.rect, window.title, ax_windows[window.pid]
                    )
                    minimized = match is not None and match.minimized
                off_screen.append((window, minimized))

            result: list[DesktopWindow] = []
            focused_given = False
            for window in on_screen:
                if window.window_id not in app_ids:
                    result.append(
                        _desktop_window(
                            window, len(result), False, False, _CURRENT, False
                        )
                    )
                    continue
                focused = not focused_given and window.pid == front_pid
                focused_given = focused_given or focused
                result.append(
                    _desktop_window(window, len(result), False, focused, _CURRENT)
                )
            for window, minimized in off_screen:
                # Minimised windows and hidden apps belong to no Space; they
                # read as current so the policy reports ``window_minimized``.
                desktop = _CURRENT if minimized else _OTHER
                result.append(
                    _desktop_window(window, len(result), minimized, False, desktop)
                )
            return DesktopInfo(current=None, count=None), result

    def _desktop_window(
        window: CgWindow,
        z: int,
        minimized: bool,
        focused: bool,
        desktop: WindowDesktop,
        capturable: bool = True,
    ) -> DesktopWindow:
        return DesktopWindow(
            window_id=str(window.window_id),
            pid=window.pid,
            process=normalize_process_name(window.owner) or "",
            title=window.title,
            rect=window.rect,
            minimized=minimized,
            focused=focused,
            z=z,
            desktop=desktop,
            capturable=capturable,
        )

    INVENTORY: WindowInventoryBackend = _MacInventory()
