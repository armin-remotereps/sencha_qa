"""Whether this Windows session can show and capture windows.

``SESSION.problem()`` answers ``not_interactive`` when nothing a person sees can be captured:

* the runner runs in session 0 (a Windows service);
* the input desktop is not ``Default`` (locked workstation, UAC prompt or
  another secure desktop) or cannot be opened;
* the remote desktop session is disconnected (nothing is drawn).

Each check is a few cheap Win32 calls; a check that cannot run is skipped
(never raises), so the capture is tried and its own errors apply.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

from controller_client.capture_platform import CaptureSessionBackend
from controller_client.protocol import CaptureStatus

logger = logging.getLogger(__name__)

SESSION_ZERO_DETAIL: Final[str] = (
    "running as a Windows service in session 0; start it from the logged-in desktop"
)
SECURE_DESKTOP_DETAIL: Final[str] = (
    "the desktop is locked or a UAC or secure desktop is shown"
)
DISCONNECTED_DETAIL: Final[str] = (
    "the remote desktop session is disconnected; nothing is drawn"
)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _DESKTOP_READOBJECTS: Final[int] = 0x0001
    _UOI_NAME: Final[int] = 2
    _NAME_MAX_CHARS: Final[int] = 256
    _INPUT_DESKTOP_NAME: Final[str] = "default"
    _WTS_CURRENT_SERVER_HANDLE: Final[int] = 0
    _WTS_CURRENT_SESSION: Final[int] = 0xFFFFFFFF
    _WTS_CONNECT_STATE: Final[int] = 8
    _WTS_DISCONNECTED: Final[int] = 4

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

    _kernel32.GetCurrentProcessId.argtypes = []
    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    _kernel32.ProcessIdToSessionId.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    _user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _user32.OpenInputDesktop.restype = wintypes.HANDLE
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _user32.CloseDesktop.restype = wintypes.BOOL
    _user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetUserObjectInformationW.restype = wintypes.BOOL
    _wtsapi32.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
    _wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    _wtsapi32.WTSFreeMemory.restype = None

    def _in_session_zero() -> bool:
        session = wintypes.DWORD(0)
        if not _kernel32.ProcessIdToSessionId(
            _kernel32.GetCurrentProcessId(), ctypes.byref(session)
        ):
            return False
        return session.value == 0

    def _input_desktop_hidden() -> bool:
        desktop = _user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
        if not desktop:
            # Locked or secure desktop: this session may not open it.
            return True
        try:
            name = ctypes.create_unicode_buffer(_NAME_MAX_CHARS)
            needed = wintypes.DWORD(0)
            if not _user32.GetUserObjectInformationW(
                desktop,
                _UOI_NAME,
                name,
                ctypes.sizeof(name),
                ctypes.byref(needed),
            ):
                return False
            return name.value.lower() != _INPUT_DESKTOP_NAME
        finally:
            _user32.CloseDesktop(desktop)

    def _session_disconnected() -> bool:
        buffer = ctypes.c_void_p()
        size = wintypes.DWORD(0)
        if not _wtsapi32.WTSQuerySessionInformationW(
            _WTS_CURRENT_SERVER_HANDLE,
            _WTS_CURRENT_SESSION,
            _WTS_CONNECT_STATE,
            ctypes.byref(buffer),
            ctypes.byref(size),
        ):
            return False
        try:
            if not buffer.value or size.value < ctypes.sizeof(ctypes.c_int):
                return False
            state = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int)).contents.value
            return int(state) == _WTS_DISCONNECTED
        finally:
            _wtsapi32.WTSFreeMemory(buffer)

    class _Win32Session:
        def problem(self) -> tuple[CaptureStatus, str] | None:
            try:
                if _in_session_zero():
                    return CaptureStatus.NOT_INTERACTIVE, SESSION_ZERO_DETAIL
                if _session_disconnected():
                    return CaptureStatus.NOT_INTERACTIVE, DISCONNECTED_DETAIL
                if _input_desktop_hidden():
                    return CaptureStatus.NOT_INTERACTIVE, SECURE_DESKTOP_DETAIL
            except Exception:  # never raise: the capture's own errors apply
                logger.debug("Windows session check failed", exc_info=True)
            return None

    SESSION: CaptureSessionBackend = _Win32Session()

else:

    class _UnsupportedSession:
        def problem(self) -> tuple[CaptureStatus, str] | None:
            return CaptureStatus.UNSUPPORTED, "this session check needs Windows"

    SESSION: CaptureSessionBackend = _UnsupportedSession()
