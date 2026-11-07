"""Window and screen images on Windows.

All rects are physical virtual-screen pixels
(``win32_windows.per_monitor_dpi``):

* ``grab_rect``: Pillow ``ImageGrab.grab(bbox, all_screens=True)`` with
  layered windows included (``CAPTUREBLT``), so the app's own menus,
  dropdowns and tooltips, which are usually layered, are in the shot. Pillow
  reads the screen per-monitor DPI aware, the same space as the rects.
* ``is_unobscured``: ``window_capture_policy.unobscured_by_stacking`` over
  the inventory (``EnumWindows`` z-order with every visible window: tool,
  owned and untitled windows included), so any window of another process
  above it that overlaps its rect counts.
* ``grab_window_only``: ``PrintWindow(PW_RENDERFULLCONTENT)`` into a GDI
  bitmap, cropped to the visible frame. The blank check is the service's.
* ``monitor_rect``: ``MonitorFromRect`` (nearest is primary) + ``rcMonitor``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Final

from PIL import Image

from controller_client.capture_platform import WindowGrabBackend
from controller_client.protocol import (
    CAPTURE_REGION_MAX_PIXELS_PER_AXIS,
    CaptureStatus,
)
from controller_client.window_capture_policy import unobscured_by_stacking
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopWindow,
    Rect,
)


def _window_hwnd(window: DesktopWindow) -> int:
    try:
        return int(window.window_id)
    except ValueError as e:
        raise CaptureUnavailableError(
            CaptureStatus.WINDOW_NOT_FOUND, "the window id is not valid"
        ) from e


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    from PIL import ImageGrab

    from controller_client.win32_windows import (
        outer_window_rect,
        per_monitor_dpi,
        window_rect,
    )

    _PW_RENDERFULLCONTENT: Final[int] = 0x2
    _MONITOR_DEFAULTTOPRIMARY: Final[int] = 1
    _BI_RGB: Final[int] = 0
    _DIB_RGB_COLORS: Final[int] = 0
    _BYTES_PER_PIXEL: Final[int] = 4

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", _BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3),
        ]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    _user32.MonitorFromRect.restype = wintypes.HMONITOR
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _user32.PrintWindow.restype = wintypes.BOOL
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    ]
    _gdi32.GetDIBits.restype = ctypes.c_int

    def _failed(detail: str) -> CaptureUnavailableError:
        return CaptureUnavailableError(CaptureStatus.CAPTURE_FAILED, detail)

    def _print_window(hwnd: int, width: int, height: int) -> Image.Image:
        """``hwnd``'s own content, ``width`` x ``height`` physical pixels."""
        screen_dc = _user32.GetDC(None)
        if not screen_dc:
            raise _failed("the screen could not be read")
        memory_dc = None
        bitmap = None
        try:
            memory_dc = _gdi32.CreateCompatibleDC(screen_dc)
            bitmap = _gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not memory_dc or not bitmap:
                raise _failed("no memory for the window image")
            previous = _gdi32.SelectObject(memory_dc, bitmap)
            try:
                printed = bool(
                    _user32.PrintWindow(hwnd, memory_dc, _PW_RENDERFULLCONTENT)
                )
            finally:
                # GetDIBits needs the bitmap out of every device context.
                _gdi32.SelectObject(memory_dc, previous)
            if not printed:
                raise CaptureUnavailableError(
                    CaptureStatus.WINDOW_OBSCURED,
                    "the covered window could not be drawn on its own; "
                    "bring it to the front",
                )
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down rows
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 8 * _BYTES_PER_PIXEL
            info.bmiHeader.biCompression = _BI_RGB
            buffer = ctypes.create_string_buffer(width * height * _BYTES_PER_PIXEL)
            lines = _gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                buffer,
                ctypes.byref(info),
                _DIB_RGB_COLORS,
            )
            if lines != height:
                raise _failed("the window image could not be read")
            return Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
        finally:
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            if memory_dc:
                _gdi32.DeleteDC(memory_dc)
            _user32.ReleaseDC(None, screen_dc)

    class _Win32Grab:
        def grab_rect(self, rect: Rect) -> Image.Image:
            if rect.is_empty:
                raise _failed("the area to capture is empty")
            try:
                image = ImageGrab.grab(
                    bbox=(rect.x, rect.y, rect.right, rect.bottom),
                    include_layered_windows=True,
                    all_screens=True,
                )
            except OSError as e:
                raise _failed("the screen could not be read") from e
            return image if image.mode == "RGB" else image.convert("RGB")

        def grab_window_only(self, window: DesktopWindow) -> Image.Image:
            if window.minimized:
                raise CaptureUnavailableError(
                    CaptureStatus.WINDOW_MINIMIZED, "the window is minimised"
                )
            hwnd = _window_hwnd(window)
            with per_monitor_dpi():
                if not _user32.IsWindow(hwnd):
                    raise CaptureUnavailableError(
                        CaptureStatus.WINDOW_NOT_FOUND, "the window was closed"
                    )
                outer = outer_window_rect(hwnd)
                visible = window_rect(hwnd)
                if outer is None or visible is None or outer.is_empty:
                    raise _failed("the window size could not be read")
                if (
                    outer.width > CAPTURE_REGION_MAX_PIXELS_PER_AXIS
                    or outer.height > CAPTURE_REGION_MAX_PIXELS_PER_AXIS
                ):
                    raise _failed("the window is too large to capture")
                image = _print_window(hwnd, outer.width, outer.height)
            # PrintWindow draws the whole window rect, invisible borders
            # included; keep the visible frame the inventory reports.
            frame = visible.intersection(outer)
            if frame.is_empty or frame == outer:
                return image
            return image.crop(
                (
                    frame.x - outer.x,
                    frame.y - outer.y,
                    frame.right - outer.x,
                    frame.bottom - outer.y,
                )
            )

        def is_unobscured(
            self, window: DesktopWindow, windows: Sequence[DesktopWindow]
        ) -> bool:
            return unobscured_by_stacking(window, windows)

        def monitor_rect(self, window: DesktopWindow) -> Rect:
            rect = window.rect
            value = wintypes.RECT(rect.x, rect.y, rect.right, rect.bottom)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            with per_monitor_dpi():
                monitor = _user32.MonitorFromRect(
                    ctypes.byref(value), _MONITOR_DEFAULTTOPRIMARY
                )
                if not monitor or not _user32.GetMonitorInfoW(
                    monitor, ctypes.byref(info)
                ):
                    raise _failed("the monitor could not be read")
            bounds = info.rcMonitor
            return Rect(
                int(bounds.left),
                int(bounds.top),
                int(bounds.right - bounds.left),
                int(bounds.bottom - bounds.top),
            )

    GRAB: WindowGrabBackend = _Win32Grab()

else:

    class _UnsupportedGrab:
        def _refuse(self) -> CaptureUnavailableError:
            return CaptureUnavailableError(
                CaptureStatus.UNSUPPORTED, "this capture needs Windows"
            )

        def grab_rect(self, rect: Rect) -> Image.Image:
            raise self._refuse()

        def grab_window_only(self, window: DesktopWindow) -> Image.Image:
            raise self._refuse()

        def is_unobscured(
            self, window: DesktopWindow, windows: Sequence[DesktopWindow]
        ) -> bool:
            return False

        def monitor_rect(self, window: DesktopWindow) -> Rect:
            raise self._refuse()

    GRAB: WindowGrabBackend = _UnsupportedGrab()
