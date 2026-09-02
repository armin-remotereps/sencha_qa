"""Refuse synthesized input that Windows would silently discard.

User Interface Privilege Isolation (UIPI) drops mouse and keyboard events a
process injects while the foreground window belongs to a process at a higher
integrity level: an elevated Task Manager, an installer, anything that went
through a UAC prompt. The Win32 calls pyautogui relies on (``mouse_event``,
``keybd_event``) return normally in that case, so without this check the
controller would report clicks and key presses that never happened.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from controller_client.exceptions import InputBlockedError

logger = logging.getLogger(__name__)

INTEGRITY_UNTRUSTED: Final[int] = 0x0000
INTEGRITY_LOW: Final[int] = 0x1000
INTEGRITY_MEDIUM: Final[int] = 0x2000
INTEGRITY_HIGH: Final[int] = 0x3000
INTEGRITY_SYSTEM: Final[int] = 0x4000

_INTEGRITY_NAMES: Final[dict[int, str]] = {
    INTEGRITY_UNTRUSTED: "untrusted",
    INTEGRITY_LOW: "low",
    INTEGRITY_MEDIUM: "medium",
    INTEGRITY_HIGH: "high (elevated)",
    INTEGRITY_SYSTEM: "system",
}

_REMEDY: Final[str] = (
    "Run the controller client as Administrator, or bring a non-elevated "
    "window to the foreground before retrying."
)


@dataclass(frozen=True)
class ForegroundWindow:
    title: str
    pid: int
    # None when the owning process refused even a limited query, which only
    # happens for processes running with higher privileges than ours.
    integrity_level: int | None


@dataclass(frozen=True)
class InputProbe:
    own_integrity_level: int
    foreground: ForegroundWindow | None


ProbeFn = Callable[[], InputProbe | None]


def describe_integrity_level(level: int) -> str:
    return _INTEGRITY_NAMES.get(level, f"0x{level:04x}")


def input_block_reason(probe: InputProbe) -> str | None:
    window = probe.foreground
    if window is None:
        return None
    own = describe_integrity_level(probe.own_integrity_level)
    target = f"the foreground window {window.title!r} (pid {window.pid})"
    if window.integrity_level is None:
        return (
            f"Windows will discard synthesized input: {target} belongs to a "
            f"process the controller is not allowed to inspect, so it runs with "
            f"higher privileges than the controller client ({own} integrity). "
            f"{_REMEDY}"
        )
    if window.integrity_level <= probe.own_integrity_level:
        return None
    return (
        f"Windows will discard synthesized input: {target} runs at "
        f"{describe_integrity_level(window.integrity_level)} integrity, above "
        f"the controller client's {own}. {_REMEDY}"
    )


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
    _TOKEN_QUERY: Final[int] = 0x0008
    _TOKEN_INTEGRITY_LEVEL: Final[int] = 25
    _ERROR_ACCESS_DENIED: Final[int] = 5
    _TITLE_MAX_CHARS: Final[int] = 512

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    _advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    _advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    def _token_integrity_level(token: wintypes.HANDLE) -> int:
        size = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size)
        )
        buffer = ctypes.create_string_buffer(size.value)
        ok = _advapi32.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, buffer, size, ctypes.byref(size)
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_MANDATORY_LABEL starts with a SID_AND_ATTRIBUTES whose first
        # field is the PSID; the integrity RID is the SID's last sub-authority.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        count = int(_advapi32.GetSidSubAuthorityCount(sid)[0])
        return int(_advapi32.GetSidSubAuthority(sid, count - 1)[0])

    def _process_integrity_level(process: wintypes.HANDLE) -> int | None:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            error = ctypes.get_last_error()
            if error == _ERROR_ACCESS_DENIED:
                return None
            raise ctypes.WinError(error)
        try:
            return _token_integrity_level(token)
        finally:
            _kernel32.CloseHandle(token)

    def _pid_integrity_level(pid: int) -> int | None:
        process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            error = ctypes.get_last_error()
            if error == _ERROR_ACCESS_DENIED:
                return None
            raise ctypes.WinError(error)
        try:
            return _process_integrity_level(process)
        finally:
            _kernel32.CloseHandle(process)

    def _foreground_window() -> ForegroundWindow | None:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(_TITLE_MAX_CHARS)
        _user32.GetWindowTextW(hwnd, title, _TITLE_MAX_CHARS)
        return ForegroundWindow(
            title=title.value,
            pid=pid.value,
            integrity_level=_pid_integrity_level(pid.value),
        )

    def probe_foreground_input() -> InputProbe | None:
        try:
            own = _process_integrity_level(_kernel32.GetCurrentProcess())
            if own is None:
                return None
            return InputProbe(own_integrity_level=own, foreground=_foreground_window())
        except OSError as e:
            logger.debug("Could not determine input integrity levels: %s", e)
            return None

else:

    def probe_foreground_input() -> InputProbe | None:
        return None


def ensure_input_not_blocked(probe: ProbeFn = probe_foreground_input) -> None:
    """Raise InputBlockedError when the OS would drop injected input right now."""
    snapshot = probe()
    if snapshot is None:
        return
    reason = input_block_reason(snapshot)
    if reason is not None:
        raise InputBlockedError(reason)
