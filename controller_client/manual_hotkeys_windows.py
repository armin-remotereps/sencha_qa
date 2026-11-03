"""Manual run Pass / Fail shortcuts on Windows (``RegisterHotKey``).

A hotkey registered with a NULL window belongs to the thread that registered
it: Windows posts ``WM_HOTKEY`` to that thread's message queue, and only that
thread may unregister it. So one daemon thread owns both registrations and
runs a ``GetMessageW`` loop; the Tk thread never calls ``RegisterHotKey``
itself but posts ``WM_APP`` messages to it (enable, disable) and ``WM_QUIT``
to stop it. The registration is exclusive, so the combination is swallowed
and the app in front never sees it. ``WM_HOTKEY`` only puts a ``HotkeyKind``
on the queue; the window drains it on the Tk thread.

The module imports on every platform; every Win32 call sits behind a
``sys.platform == "win32"`` check (mypy skips those branches elsewhere).
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from typing import Final

from controller_client.manual_hotkeys_base import (
    OFF_STATUS,
    HotkeyKind,
    HotkeyQueue,
    HotkeyState,
    HotkeyStatus,
    default_triggers,
    focus_only,
)

logger = logging.getLogger(__name__)

_MOD_CONTROL: Final[int] = 0x0002
_MOD_SHIFT: Final[int] = 0x0004
# Holding the combination down sends one WM_HOTKEY, not one per auto-repeat.
_MOD_NOREPEAT: Final[int] = 0x4000
_MODIFIERS: Final[int] = _MOD_CONTROL | _MOD_SHIFT | _MOD_NOREPEAT

_WM_USER: Final[int] = 0x0400
_WM_QUIT: Final[int] = 0x0012
_WM_HOTKEY: Final[int] = 0x0312
_WM_APP: Final[int] = 0x8000
_WM_APP_ENABLE: Final[int] = _WM_APP + 1
_WM_APP_DISABLE: Final[int] = _WM_APP + 2
_PM_NOREMOVE: Final[int] = 0x0000

_ERROR_HOTKEY_ALREADY_REGISTERED: Final[int] = 1409

# Hotkey ids are private to the registering thread; any 0x0000-0xBFFF works.
_HOTKEY_IDS: Final[dict[int, HotkeyKind]] = {1: HotkeyKind.PASS, 2: HotkeyKind.FAIL}
# Virtual-key codes of the letters are their upper-case ASCII codes.
_VIRTUAL_KEYS: Final[dict[HotkeyKind, int]] = {
    HotkeyKind.PASS: ord("P"),
    HotkeyKind.FAIL: ord("F"),
}

# The thread creates its message queue at once; this only bounds a stuck start.
_THREAD_READY_TIMEOUT_S: Final[float] = 2.0
_THREAD_JOIN_TIMEOUT_S: Final[float] = 1.0


def _registration_status(errors: dict[HotkeyKind, int]) -> HotkeyStatus:
    """ACTIVE when both registered, else focus_only naming what failed.

    ``errors`` maps each kind that failed to its Win32 error code.
    """
    if not errors:
        return HotkeyStatus(state=HotkeyState.ACTIVE)
    triggers = default_triggers()
    taken = [
        triggers[kind]
        for kind, error in errors.items()
        if error == _ERROR_HOTKEY_ALREADY_REGISTERED
    ]
    failed = [
        f"{triggers[kind]} (Windows error {error})"
        for kind, error in errors.items()
        if error != _ERROR_HOTKEY_ALREADY_REGISTERED
    ]
    reasons: list[str] = []
    if len(taken) == 1:
        reasons.append(f"{taken[0]} is used by another app")
    elif taken:
        reasons.append(f"{' and '.join(taken)} are used by other apps")
    if failed:
        reasons.append(f"Could not register {' and '.join(failed)}")
    return focus_only("; ".join(reasons))


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.RegisterHotKey.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def _init_message_queue() -> int:
        """Create the calling thread's message queue; return its thread id."""
        msg = wintypes.MSG()
        # Any message call creates the queue; until then posting to the
        # thread fails.
        _user32.PeekMessageW(ctypes.byref(msg), None, _WM_USER, _WM_USER, _PM_NOREMOVE)
        return int(_kernel32.GetCurrentThreadId())

    def _pump(handle: Callable[[int, int], None]) -> None:
        """Run the thread's GetMessageW loop until WM_QUIT.

        ``handle`` gets each thread message and its wParam. Raises OSError
        when the loop itself fails.
        """
        msg = wintypes.MSG()
        while True:
            result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0:
                return
            if result == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            handle(int(msg.message), int(msg.wParam))

    def _post_thread_message(thread_id: int, message: int) -> int:
        """Post to a thread's queue; return 0 or the Win32 error code."""
        if _user32.PostThreadMessageW(thread_id, message, 0, 0):
            return 0
        return ctypes.get_last_error() or -1

    def _register_hotkey(hotkey_id: int, virtual_key: int) -> int:
        """Register for the calling thread; return 0 or the Win32 error code."""
        if _user32.RegisterHotKey(None, hotkey_id, _MODIFIERS, virtual_key):
            return 0
        return ctypes.get_last_error() or -1

    def _unregister_hotkey(hotkey_id: int) -> int:
        """Release the calling thread's hotkey; return 0 or the error code."""
        if _user32.UnregisterHotKey(None, hotkey_id):
            return 0
        return ctypes.get_last_error() or -1

else:
    # Never called: WindowsHotkeys is built only on win32 (manual_hotkeys).

    def _init_message_queue() -> int:
        raise OSError("Windows only")

    def _pump(handle: Callable[[int, int], None]) -> None:
        raise OSError("Windows only")

    def _post_thread_message(thread_id: int, message: int) -> int:
        return -1

    def _register_hotkey(hotkey_id: int, virtual_key: int) -> int:
        return -1

    def _unregister_hotkey(hotkey_id: int) -> int:
        return 0


class WindowsHotkeys:
    """``HotkeyBackend`` for Windows; created and driven from the Tk thread.

    Status: OFF until enabled, PENDING until the hotkey thread answered, then
    ACTIVE or FOCUS_ONLY, OFF again after disable. ``dry_run`` registers and
    releases at once (``--probe-shortcuts``) and reports what it found.
    """

    def __init__(self, events: HotkeyQueue, *, dry_run: bool) -> None:
        self._events = events
        self._dry_run = dry_run
        # Written by the Tk thread (PENDING, failures to post) and the hotkey
        # thread (its answers); a reference swap of an immutable value.
        self._status: HotkeyStatus = OFF_STATUS
        # Tk thread only: what was last asked, so repeated calls post nothing.
        self._wanted = False
        self._closed = False
        # Set only once ``start()`` succeeded, so close never joins an
        # unstarted thread.
        self._thread: threading.Thread | None = None
        # Tk thread only: a start that can never succeed (the thread did not
        # start, or could not create its message queue). Sticky, so later
        # enables answer at once instead of waiting for ``_ready`` again.
        self._start_failure: HotkeyStatus | None = None
        # Set by the hotkey thread before ``_ready``; 0 when it failed.
        self._thread_id = 0
        self._ready = threading.Event()
        # Hotkey thread only: the ids registered right now.
        self._registered: set[int] = set()

    @property
    def status(self) -> HotkeyStatus:
        return self._status

    def enable(self) -> None:
        if self._closed or self._wanted:
            return
        self._wanted = True
        if self._start_failure is not None:
            self._status = self._start_failure
            return
        if not self._start_thread():
            self._status = self._start_failure or focus_only(
                "The shortcut service did not start"
            )
            return
        self._status = HotkeyStatus(state=HotkeyState.PENDING)
        if not self._post(_WM_APP_ENABLE):
            self._status = focus_only("The shortcut service did not answer")

    def disable(self) -> None:
        if self._closed or not self._wanted:
            return
        self._wanted = False
        # The thread answers OFF once it released the combinations.
        if not self._post(_WM_APP_DISABLE):
            self._status = OFF_STATUS

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wanted = False
        thread = self._thread
        if thread is not None:
            # The loop releases both registrations on its way out.
            self._post(_WM_QUIT)
            try:
                thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
            except RuntimeError:
                # close never raises (HotkeyBackend contract).
                logger.exception("Could not wait for the shortcut thread")
            if thread.is_alive():
                logger.warning("The shortcut thread did not stop in time")
        self._status = OFF_STATUS

    def _start_thread(self) -> bool:
        """Start the hotkey thread once and wait until it can take messages.

        Records ``_start_failure`` when the start can never succeed.
        """
        if self._thread is None:
            thread = threading.Thread(
                target=self._run, name="manual-hotkeys", daemon=True
            )
            try:
                thread.start()
            except RuntimeError:
                logger.exception("Could not start the shortcut thread")
                self._start_failure = focus_only("The shortcut service did not start")
                return False
            self._thread = thread
        # PostThreadMessageW fails until the thread has a message queue.
        if not self._ready.wait(timeout=_THREAD_READY_TIMEOUT_S):
            return False
        if self._thread_id == 0:
            # The thread could not create its message queue and has ended.
            self._start_failure = focus_only("The shortcut service did not start")
            return False
        return True

    def _post(self, message: int) -> bool:
        if not self._thread_id:
            return False
        try:
            error = _post_thread_message(self._thread_id, message)
        except OSError:
            logger.exception("Could not post to the shortcut thread")
            return False
        if error:
            logger.warning(
                "Could not post to the shortcut thread (Windows error %s)", error
            )
            return False
        return True

    def _run(self) -> None:
        """The hotkey thread: own the registrations and their messages."""
        try:
            self._thread_id = _init_message_queue()
        except OSError:
            logger.exception("Could not set up the shortcut thread")
            return
        finally:
            self._ready.set()
        try:
            _pump(self._handle)
        except Exception:
            logger.exception("The shortcut thread failed")
            self._status = focus_only("The shortcut service stopped")
        finally:
            self._unregister()

    def _handle(self, message: int, wparam: int) -> None:
        if message == _WM_HOTKEY:
            kind = _HOTKEY_IDS.get(wparam)
            if kind is not None:
                self._events.put(kind)
        elif message == _WM_APP_ENABLE:
            self._status = self._register()
            if self._dry_run:
                self._unregister()
        elif message == _WM_APP_DISABLE:
            self._unregister()
            self._status = OFF_STATUS

    def _register(self) -> HotkeyStatus:
        """Register what is not registered yet (hotkey thread only)."""
        errors: dict[HotkeyKind, int] = {}
        for hotkey_id, kind in _HOTKEY_IDS.items():
            if hotkey_id in self._registered:
                continue
            error = _register_hotkey(hotkey_id, _VIRTUAL_KEYS[kind])
            if error:
                errors[kind] = error
            else:
                self._registered.add(hotkey_id)
        if errors:
            logger.info("Manual shortcuts not registered: %s", errors)
        # One combination failing keeps the other registered system-wide.
        return _registration_status(errors)

    def _unregister(self) -> None:
        """Release every registration (hotkey thread only)."""
        for hotkey_id in sorted(self._registered):
            error = _unregister_hotkey(hotkey_id)
            if error:
                logger.debug(
                    "UnregisterHotKey(%s) failed (Windows error %s)", hotkey_id, error
                )
        self._registered.clear()
