"""Windows virtual desktops: which desktop a window is on, and their order.

Two sources:

* The documented ``IVirtualDesktopManager`` (shobjidl_core.h), created with
  ``CoCreateInstance`` and called through its vtable with ctypes. It alone
  decides whether a window is on the current desktop
  (``IsWindowOnCurrentVirtualDesktop``) and which desktop it is on
  (``GetWindowDesktopId``). COM is initialised per call on the calling
  worker thread (``open_manager``) and always released.
* Read-only registry values Explorer keeps under
  ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VirtualDesktops``:
  ``VirtualDesktopIDs`` (the desktops in the order Task View shows them),
  ``Desktops\\{GUID}\\Name`` (a name the person gave a desktop) and
  ``CurrentVirtualDesktop`` (Windows 11; Windows 10 keeps it under
  ``SessionInfo\\<session id>\\VirtualDesktops``). They are undocumented: any
  value that cannot be read gives None (or no name), never an error. The
  undocumented ``IVirtualDesktopManagerInternal`` is not used.

Windows only: everything is defined under ``sys.platform == "win32"``, and
only the Windows capture modules import it.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    import ctypes
    import winreg
    from ctypes import wintypes

    _REGISTRY_PATH: Final[str] = (
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops"
    )
    _SESSION_REGISTRY_PATH: Final[str] = (
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\SessionInfo"
        r"\{}\VirtualDesktops"
    )
    _GUID_BYTES: Final[int] = 16

    _CLSID_VIRTUAL_DESKTOP_MANAGER: Final[uuid.UUID] = uuid.UUID(
        "aa509086-5ca9-4c25-8f95-589d3c07b48a"
    )
    _IID_IVIRTUAL_DESKTOP_MANAGER: Final[uuid.UUID] = uuid.UUID(
        "a5cd92ff-29be-454c-8d04-d82879fb3f1b"
    )
    _COINIT_MULTITHREADED: Final[int] = 0x0
    _CLSCTX_ALL: Final[int] = 0x17
    _S_OK: Final[int] = 0
    _S_FALSE: Final[int] = 1
    # 0x80010106 as a signed HRESULT: the thread already has another
    # apartment model; COM is usable but this call must not uninitialise it.
    _RPC_E_CHANGED_MODE: Final[int] = -2147417850
    # IUnknown (QueryInterface, AddRef, Release), then the manager's methods.
    _VTBL_RELEASE: Final[int] = 2
    _VTBL_IS_ON_CURRENT: Final[int] = 3
    _VTBL_GET_DESKTOP_ID: Final[int] = 4

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def _to_guid(value: uuid.UUID) -> _GUID:
        return _GUID.from_buffer_copy(value.bytes_le)

    def _from_guid(value: _GUID) -> uuid.UUID:
        return uuid.UUID(
            bytes_le=ctypes.string_at(ctypes.addressof(value), _GUID_BYTES)
        )

    _ole32 = ctypes.WinDLL("ole32")
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoUninitialize.restype = None
    _ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ole32.CoCreateInstance.restype = ctypes.c_long
    _kernel32.GetCurrentProcessId.argtypes = []
    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    _kernel32.ProcessIdToSessionId.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.ProcessIdToSessionId.restype = wintypes.BOOL

    _ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    _IsOnCurrentProto = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.POINTER(wintypes.BOOL)
    )
    _GetDesktopIdProto = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.POINTER(_GUID)
    )

    class VirtualDesktopManager:
        """One ``IVirtualDesktopManager`` instance; use it only inside the
        ``open_manager`` block (and thread) that created it."""

        def __init__(self, pointer: ctypes.c_void_p) -> None:
            self._pointer = pointer
            vtable = ctypes.cast(
                pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            ).contents
            self._release = _ReleaseProto(vtable[_VTBL_RELEASE])
            self._is_on_current = _IsOnCurrentProto(vtable[_VTBL_IS_ON_CURRENT])
            self._get_desktop_id = _GetDesktopIdProto(vtable[_VTBL_GET_DESKTOP_ID])

        def is_on_current(self, hwnd: int) -> bool | None:
            """Whether ``hwnd`` is on the current desktop (a window pinned to
            all desktops is); None when Windows cannot tell (not a tracked
            top-level window, Explorer restarting)."""
            on_current = wintypes.BOOL(0)
            result = self._is_on_current(self._pointer, hwnd, ctypes.byref(on_current))
            if result != _S_OK:
                return None
            return bool(on_current.value)

        def desktop_id(self, hwnd: int) -> uuid.UUID | None:
            """The id of ``hwnd``'s desktop; None when unknown (GUID_NULL)."""
            guid = _GUID()
            result = self._get_desktop_id(self._pointer, hwnd, ctypes.byref(guid))
            if result != _S_OK:
                return None
            value = _from_guid(guid)
            return None if value.int == 0 else value

        def release(self) -> None:
            self._release(self._pointer)

    @contextmanager
    def open_manager() -> Iterator[VirtualDesktopManager | None]:
        """Initialise COM on this thread and create the manager.

        Yields None when COM or the manager is unavailable (Explorer not
        running, Windows Server without the shell). Releases the instance and
        uninitialises COM on exit when this call initialised it.
        """
        init_result = int(_ole32.CoInitializeEx(None, _COINIT_MULTITHREADED))
        uninitialise = init_result in (_S_OK, _S_FALSE)
        if not uninitialise and init_result != _RPC_E_CHANGED_MODE:
            logger.debug("CoInitializeEx failed (0x%08x)", init_result & 0xFFFFFFFF)
            yield None
            return
        manager: VirtualDesktopManager | None = None
        try:
            pointer = ctypes.c_void_p()
            clsid = _to_guid(_CLSID_VIRTUAL_DESKTOP_MANAGER)
            iid = _to_guid(_IID_IVIRTUAL_DESKTOP_MANAGER)
            result = int(
                _ole32.CoCreateInstance(
                    ctypes.byref(clsid),
                    None,
                    _CLSCTX_ALL,
                    ctypes.byref(iid),
                    ctypes.byref(pointer),
                )
            )
            if result == _S_OK and pointer.value:
                manager = VirtualDesktopManager(pointer)
            else:
                logger.debug(
                    "IVirtualDesktopManager unavailable (0x%08x)",
                    result & 0xFFFFFFFF,
                )
            yield manager
        finally:
            if manager is not None:
                manager.release()
            if uninitialise:
                _ole32.CoUninitialize()

    def _read_value(path: str, name: str) -> object | None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        result: object = value
        return result

    def _guid_value(raw: object | None) -> uuid.UUID | None:
        if not isinstance(raw, bytes) or len(raw) != _GUID_BYTES:
            return None
        value = uuid.UUID(bytes_le=raw)
        return None if value.int == 0 else value

    def desktop_order() -> tuple[uuid.UUID, ...] | None:
        """The desktops in Task View order (index 0 is desktop 1); None when
        the registry value is missing or malformed."""
        raw = _read_value(_REGISTRY_PATH, "VirtualDesktopIDs")
        if not isinstance(raw, bytes) or not raw or len(raw) % _GUID_BYTES:
            return None
        return tuple(
            uuid.UUID(bytes_le=raw[start : start + _GUID_BYTES])
            for start in range(0, len(raw), _GUID_BYTES)
        )

    def desktop_names(order: tuple[uuid.UUID, ...]) -> tuple[str, ...]:
        """Each desktop's own name ("" when unnamed: Windows shows "Desktop
        N"), in ``order``; empty when no desktop has a name."""
        names: list[str] = []
        for desktop in order:
            path = f"{_REGISTRY_PATH}\\Desktops\\{{{str(desktop).upper()}}}"
            raw = _read_value(path, "Name")
            names.append(raw.strip() if isinstance(raw, str) else "")
        return tuple(names) if any(names) else ()

    def _session_id() -> int | None:
        session = wintypes.DWORD(0)
        if not _kernel32.ProcessIdToSessionId(
            _kernel32.GetCurrentProcessId(), ctypes.byref(session)
        ):
            return None
        return int(session.value)

    def registry_current_desktop() -> uuid.UUID | None:
        """The current desktop's id as Explorer last wrote it (Windows 11
        location first, then Windows 10's per-session one); None when
        unreadable."""
        current = _guid_value(_read_value(_REGISTRY_PATH, "CurrentVirtualDesktop"))
        if current is not None:
            return current
        session = _session_id()
        if session is None:
            return None
        return _guid_value(
            _read_value(_SESSION_REGISTRY_PATH.format(session), "CurrentVirtualDesktop")
        )
