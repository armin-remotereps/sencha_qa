"""What the OS reports about the main display's size and scale.

Used only by ``diagnose --input`` to cross-check the backend's capture and
input sizes; the runtime action path never imports it. Every reader is best
effort: a failing OS call yields an ``unavailable`` result, never an exception.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final

if sys.platform == "darwin":
    import Quartz
elif sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

# Sizes are compared with this tolerance, per axis, in pixels.
_TOLERANCE_PX: Final = 1

# compare_spaces verdicts.
VERDICT_OK: Final = "OK"
VERDICT_MISMATCH: Final = "MISMATCH"
NO_REFERENCE: Final = "NO REFERENCE"


@dataclass(frozen=True)
class DisplayScale:
    # Where the facts came from, e.g. "Quartz display mode" or "not read".
    source: str
    # The main/primary display in physical pixels.
    physical_size: tuple[int, int] | None
    # The size pointer input should report given the OS scale and this
    # process's DPI awareness.
    expected_input_size: tuple[int, int] | None
    # macOS backing scale; Windows true monitor effective DPI / 96.
    scale: float | None
    # The raw numbers, for the summary line.
    detail: str


def _unavailable(reason: str) -> DisplayScale:
    return DisplayScale(
        source=f"unavailable: {reason}",
        physical_size=None,
        expected_input_size=None,
        scale=None,
        detail="",
    )


if sys.platform == "darwin":

    def _read() -> DisplayScale:
        mode = Quartz.CGDisplayCopyDisplayMode(Quartz.CGMainDisplayID())
        if mode is None:
            return _unavailable("CGDisplayCopyDisplayMode returned no mode")
        pixels = (
            int(Quartz.CGDisplayModeGetPixelWidth(mode)),
            int(Quartz.CGDisplayModeGetPixelHeight(mode)),
        )
        points = (
            int(Quartz.CGDisplayModeGetWidth(mode)),
            int(Quartz.CGDisplayModeGetHeight(mode)),
        )
        scale = pixels[0] / points[0] if points[0] else None
        scale_text = f"{scale:.2f}" if scale is not None else "unknown"
        return DisplayScale(
            source="Quartz display mode",
            physical_size=pixels,
            expected_input_size=points,
            scale=scale,
            detail=(
                f"{pixels[0]}x{pixels[1]} px / {points[0]}x{points[1]} pt "
                f"scale {scale_text}"
            ),
        )

elif sys.platform == "win32":
    _ENUM_CURRENT_SETTINGS: Final = 0xFFFFFFFF
    _MONITOR_DEFAULTTOPRIMARY: Final = 1
    _MDT_EFFECTIVE_DPI: Final = 0
    _LOGPIXELSX: Final = 88
    _DEFAULT_DPI: Final = 96
    # DPI_AWARENESS_CONTEXT pseudo handles.
    _CONTEXT_PER_MONITOR_AWARE_V2: Final = -4
    _CONTEXT_PER_MONITOR_AWARE: Final = -3
    _AWARENESS_NAMES: Final[dict[int, str]] = {
        0: "unaware",
        1: "system",
        2: "per-monitor",
    }

    class _DEVMODEW(ctypes.Structure):
        # The display variant of DEVMODEW: the printer/display union is laid
        # out as its display member (dmPosition + two DWORDs, 16 bytes).
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            ("dmPosition", wintypes.POINTL),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD),
            ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD),
            ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD),
            ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD),
            ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD),
            ("dmPanningHeight", wintypes.DWORD),
        ]

    def _physical_size(user32: ctypes.WinDLL) -> tuple[int, int]:
        # The current display mode is in physical pixels whatever this
        # process's DPI awareness (GetDeviceCaps(DESKTOPHORZRES) may not be).
        user32.EnumDisplaySettingsW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_DEVMODEW),
        ]
        user32.EnumDisplaySettingsW.restype = wintypes.BOOL
        mode = _DEVMODEW()
        mode.dmSize = ctypes.sizeof(_DEVMODEW)
        if not user32.EnumDisplaySettingsW(
            None, _ENUM_CURRENT_SETTINGS, ctypes.byref(mode)
        ):
            raise OSError("EnumDisplaySettingsW failed")
        return int(mode.dmPelsWidth), int(mode.dmPelsHeight)

    def _monitor_dpi(user32: ctypes.WinDLL) -> int:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HMONITOR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long  # HRESULT
        monitor = user32.MonitorFromPoint(
            wintypes.POINT(0, 0), _MONITOR_DEFAULTTOPRIMARY
        )
        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        result = shcore.GetDpiForMonitor(
            monitor, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
        )
        if result != 0:
            raise OSError(f"GetDpiForMonitor failed (0x{result & 0xFFFFFFFF:08x})")
        return int(dpi_x.value)

    def _true_monitor_dpi(user32: ctypes.WinDLL) -> tuple[int, str]:
        # GetDpiForMonitor is virtualized by the calling thread's awareness:
        # a system-aware thread (pyautogui calls SetProcessDPIAware) gets the
        # system DPI back. Read it under a temporary per-monitor context.
        # Returns the DPI and how it was read.
        try:
            set_context = user32.SetThreadDpiAwarenessContext
        except AttributeError:
            # Before Windows 10 1607: no thread contexts, so the value may be
            # the system DPI rather than the monitor's own.
            return (
                _monitor_dpi(user32),
                "read as this thread sees it (no SetThreadDpiAwarenessContext; "
                "may be the system DPI)",
            )
        # DPI_AWARENESS_CONTEXT is a handle; NULL means the call failed.
        set_context.argtypes = [ctypes.c_ssize_t]
        set_context.restype = ctypes.c_ssize_t
        how = "per-monitor v2"
        previous = set_context(_CONTEXT_PER_MONITOR_AWARE_V2)
        if not previous:
            # Before Windows 10 1703 (no v2).
            how = "per-monitor v1"
            previous = set_context(_CONTEXT_PER_MONITOR_AWARE)
            if not previous:
                raise OSError("SetThreadDpiAwarenessContext failed")
        try:
            return _monitor_dpi(user32), f"read {how}"
        finally:
            set_context(previous)

    def _awareness(user32: ctypes.WinDLL) -> int:
        # 0 unaware, 1 system, 2 per-monitor (v1 or v2).
        try:
            get_context = user32.GetThreadDpiAwarenessContext
            get_awareness = user32.GetAwarenessFromDpiAwarenessContext
        except AttributeError:
            # Before Windows 10 1607.
            user32.IsProcessDPIAware.argtypes = []
            user32.IsProcessDPIAware.restype = wintypes.BOOL
            return 1 if user32.IsProcessDPIAware() else 0
        get_context.argtypes = []
        get_context.restype = ctypes.c_void_p
        get_awareness.argtypes = [ctypes.c_void_p]
        get_awareness.restype = ctypes.c_int
        return int(get_awareness(get_context()))

    def _system_dpi(user32: ctypes.WinDLL) -> int:
        try:
            get_dpi_for_system = user32.GetDpiForSystem
        except AttributeError:
            # Before Windows 10 1607.
            gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
            user32.GetDC.argtypes = [wintypes.HWND]
            user32.GetDC.restype = wintypes.HDC
            user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            user32.ReleaseDC.restype = ctypes.c_int
            gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
            gdi32.GetDeviceCaps.restype = ctypes.c_int
            dc = user32.GetDC(None)
            if not dc:
                raise OSError("GetDC failed")
            try:
                return int(gdi32.GetDeviceCaps(dc, _LOGPIXELSX))
            finally:
                user32.ReleaseDC(None, dc)
        get_dpi_for_system.argtypes = []
        get_dpi_for_system.restype = wintypes.UINT
        return int(get_dpi_for_system())

    def _read() -> DisplayScale:
        # A private user32 instance so these argtypes never touch the
        # function pointers desktop_pyautogui configured on its own instance.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        physical = _physical_size(user32)
        monitor_dpi, dpi_read = _true_monitor_dpi(user32)
        awareness = _awareness(user32)
        system_dpi = _system_dpi(user32)
        if awareness == 2:
            process_dpi = monitor_dpi
        elif awareness == 1:
            process_dpi = system_dpi
        else:
            process_dpi = _DEFAULT_DPI
        expected = (
            round(physical[0] * process_dpi / monitor_dpi),
            round(physical[1] * process_dpi / monitor_dpi),
        )
        awareness_name = _AWARENESS_NAMES.get(awareness, f"unknown ({awareness})")
        return DisplayScale(
            source="Win32 display settings",
            physical_size=physical,
            expected_input_size=expected,
            scale=monitor_dpi / _DEFAULT_DPI,
            detail=(
                f"physical {physical[0]}x{physical[1]} | awareness "
                f"{awareness_name} | system DPI {system_dpi} | true monitor DPI "
                f"{monitor_dpi} ({dpi_read}) | expected input "
                f"{expected[0]}x{expected[1]}"
            ),
        )

else:

    def _read() -> DisplayScale:
        return DisplayScale(
            source="not read (GNOME: input space is the stream frame by design)",
            physical_size=None,
            expected_input_size=None,
            scale=None,
            detail="",
        )


def read_display_scale() -> DisplayScale:
    """Read the main display's physical size and scale from the OS.

    On Windows the expected input size depends on this process's DPI
    awareness, which importing pyautogui sets (``SetProcessDPIAware``); call
    this after the backend is loaded to see what the runner really uses.
    """
    try:
        return _read()
    except Exception as exc:
        return _unavailable(f"{type(exc).__name__}: {exc}")


def _same_size(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return abs(a[0] - b[0]) <= _TOLERANCE_PX and abs(a[1] - b[1]) <= _TOLERANCE_PX


def _fmt(size: tuple[int, int]) -> str:
    return f"{size[0]}x{size[1]}"


def compare_spaces(
    capture: tuple[int, int], input: tuple[int, int], scale: DisplayScale
) -> tuple[str, str]:
    """Check the backend's capture and input sizes against the OS facts.

    Returns ``(verdict, explanation)``. The verdict is ``OK`` when capture
    equals the physical size and input equals the expected input size (each
    where known, within 1 px per axis), ``MISMATCH`` when either differs, and
    ``NO REFERENCE`` when the OS gave neither.
    """
    if scale.physical_size is None and scale.expected_input_size is None:
        return NO_REFERENCE, (
            f"{scale.source}; capture {_fmt(capture)}, input {_fmt(input)} "
            "are not cross-checked"
        )
    problems: list[str] = []
    checks: list[str] = []
    if scale.physical_size is not None:
        if _same_size(capture, scale.physical_size):
            checks.append(f"capture {_fmt(capture)} = physical")
        else:
            problems.append(
                f"capture {_fmt(capture)} != physical {_fmt(scale.physical_size)}"
            )
    if scale.expected_input_size is not None:
        if _same_size(input, scale.expected_input_size):
            checks.append(f"input {_fmt(input)} = expected")
        else:
            problems.append(
                f"input {_fmt(input)} != expected " f"{_fmt(scale.expected_input_size)}"
            )
    if problems:
        return VERDICT_MISMATCH, f"{'; '.join(problems)} ({scale.source})"
    return VERDICT_OK, f"{', '.join(checks)} ({scale.source})"
