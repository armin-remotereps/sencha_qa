"""X11 Pass / Fail shortcuts: ``XGrabKey`` on the root window through ctypes.

A dedicated thread ``person-window-x11`` owns its own display connection
(never Tk's), grabs Ctrl+Shift+P / Ctrl+Shift+F on the root window, and waits
in ``select()`` on the connection and a wake pipe. ``enable`` / ``disable`` /
``close`` record what is wanted and wake the thread; every Xlib call runs on
that thread. The grab swallows the combinations while it is held.

Xlib reports a refused grab (another client holds it) asynchronously, so the
grabs run under a temporary error handler and ``XSync``. The handler is
process-wide and Tk keeps running while it is installed: errors of other
display connections (Tk's) go to the handler that was installed before, and
the handler is swapped back right after the ``XSync``.

libX11 older than 1.8 is not thread-safe unless ``XInitThreads`` is the first
Xlib call of the process, before Tk opens its display: the window process
calls ``prepare_x11_threads`` before it creates Tk. The backend calls it again
(idempotent), which is enough only on libX11 1.8+ (threads enabled already).
"""

import ctypes
import ctypes.util
import logging
import os
import select
import sys
import threading
from collections.abc import Mapping
from typing import Final

from controller_client.manual_hotkeys_base import (
    OFF_STATUS,
    HotkeyKind,
    HotkeyQueue,
    HotkeyState,
    HotkeyStatus,
    focus_only,
)

logger = logging.getLogger(__name__)

_SHIFT_MASK: Final = 1 << 0
_LOCK_MASK: Final = 1 << 1
_CONTROL_MASK: Final = 1 << 2
_MOD2_MASK: Final = 1 << 4  # Num Lock on every common layout
# The grab must match with Caps Lock and Num Lock on or off.
_MODIFIER_SETS: Final = tuple(
    _CONTROL_MASK | _SHIFT_MASK | extra
    for extra in (0, _LOCK_MASK, _MOD2_MASK, _LOCK_MASK | _MOD2_MASK)
)
_KEY_PRESS: Final = 2
_KEY_RELEASE: Final = 3
_GRAB_MODE_ASYNC: Final = 1
_BAD_ACCESS: Final = 10
_KEYSYMS: Final[Mapping[HotkeyKind, int]] = {
    HotkeyKind.PASS: 0x70,  # XK_p
    HotkeyKind.FAIL: 0x66,  # XK_f
}
_LABELS: Final[Mapping[HotkeyKind, str]] = {
    HotkeyKind.PASS: "Ctrl+Shift+P",
    HotkeyKind.FAIL: "Ctrl+Shift+F",
}
# enable() waits this long for the thread to report the grab, so the first
# status read already says whether it worked.
_ACK_TIMEOUT_S: Final = 0.5
# close() waits this long for the X thread to release the grab and end.
_THREAD_JOIN_TIMEOUT_S: Final = 1.0


class _XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xkey", _XKeyEvent),
        ("pad", ctypes.c_long * 24),
    ]


class _XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


_ERROR_HANDLER = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_XErrorEvent)
)


class _GrabRefusedError(Exception):
    pass


_threads_lock = threading.Lock()
_threads_prepared = False


def _open_libx11() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("X11")
    if name is None:
        return None
    try:
        return ctypes.CDLL(name)
    except OSError:
        return None


def prepare_x11_threads() -> None:
    """Call ``XInitThreads`` once, before any other Xlib call (before Tk).

    A no-op off Linux, without libX11 or when already done; never raises.
    """
    global _threads_prepared
    if not sys.platform.startswith("linux"):
        return
    with _threads_lock:
        if _threads_prepared:
            return
        _threads_prepared = True
        try:
            lib = _open_libx11()
            if lib is None:
                return
            lib.XInitThreads.argtypes = []
            lib.XInitThreads.restype = ctypes.c_int
            if not lib.XInitThreads():
                logger.warning("XInitThreads failed; X11 shortcuts may be unsafe")
        except Exception:
            logger.exception("XInitThreads could not be called")


def _load_xlib() -> ctypes.CDLL | None:
    # Too late for libX11 < 1.8 when Tk is already up (see the module
    # docstring); harmless when the window process called it first.
    prepare_x11_threads()
    lib = _open_libx11()
    if lib is None:
        return None
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XCloseDisplay.restype = ctypes.c_int
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XKeysymToKeycode.restype = ctypes.c_ubyte
    lib.XGrabKey.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.XGrabKey.restype = ctypes.c_int
    lib.XUngrabKey.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_ulong,
    ]
    lib.XUngrabKey.restype = ctypes.c_int
    lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XSync.restype = ctypes.c_int
    lib.XSetErrorHandler.argtypes = [_ERROR_HANDLER]
    lib.XSetErrorHandler.restype = _ERROR_HANDLER
    lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
    lib.XConnectionNumber.restype = ctypes.c_int
    lib.XPending.argtypes = [ctypes.c_void_p]
    lib.XPending.restype = ctypes.c_int
    lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
    lib.XNextEvent.restype = ctypes.c_int
    lib.XkbSetDetectableAutoRepeat.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    lib.XkbSetDetectableAutoRepeat.restype = ctypes.c_int
    return lib


class X11Hotkeys:
    """X11 root-window grab backend (see the module docstring)."""

    def __init__(self, events: HotkeyQueue, *, dry_run: bool) -> None:
        self._events = events
        self._dry_run = dry_run
        # Shared with the Tk thread, under _lock.
        self._lock = threading.Lock()
        self._status: HotkeyStatus = OFF_STATUS
        self._wanted = False
        self._closed = False
        self._generation = 0
        self._applied = threading.Condition(self._lock)
        self._applied_generation = 0
        # The X connection could not be set up; the thread has ended.
        self._failure: HotkeyStatus | None = None
        self._pipe_open = True
        # X thread only.
        self._lib: ctypes.CDLL | None = None
        self._display: int | None = None
        self._root = 0
        self._keycodes: dict[int, HotkeyKind] = {}
        self._grabbed = False
        self._attempted = -1
        self._down: set[int] = set()
        self._errors: list[int] = []
        # Referenced for as long as it may be installed (ctypes would free it).
        self._handler = _ERROR_HANDLER(self._on_error)
        self._previous_handler: object = None
        self._wake_read, self._wake_write = os.pipe()
        # _wake writes under _lock: never block there (a full pipe already
        # means the thread will wake).
        os.set_blocking(self._wake_write, False)
        self._thread = threading.Thread(
            target=self._run, name="person-window-x11", daemon=True
        )
        self._thread.start()

    # -- Tk thread -------------------------------------------------------

    @property
    def status(self) -> HotkeyStatus:
        with self._lock:
            return self._status

    def enable(self) -> None:
        with self._lock:
            if self._closed or self._wanted:
                return
            self._wanted = True
            if self._failure is not None:
                self._status = self._failure
                return
            self._generation += 1
            generation = self._generation
        self._wake()
        with self._applied:
            self._applied.wait_for(
                lambda: self._applied_generation >= generation or self._closed,
                timeout=_ACK_TIMEOUT_S,
            )

    def disable(self) -> None:
        with self._lock:
            if self._closed or not self._wanted:
                return
            self._wanted = False
            self._generation += 1
            self._status = OFF_STATUS
        self._wake()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._wanted = False
            self._status = OFF_STATUS
            self._applied.notify_all()
        self._wake()
        if threading.current_thread() is self._thread:
            return
        self._thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            logger.warning("The X11 shortcut thread did not stop in time")

    def _wake(self) -> None:
        with self._lock:
            if not self._pipe_open:
                return
            try:
                os.write(self._wake_write, b"\0")
            except BlockingIOError:
                logger.debug("X11 shortcut wake pipe is full")
            except OSError:
                logger.debug("X11 shortcut wake pipe write failed", exc_info=True)

    # -- X thread --------------------------------------------------------

    def _run(self) -> None:
        try:
            if self._open():
                self._loop()
        except Exception:
            logger.exception("X11 shortcut thread failed")
            self._fail(focus_only("The X11 shortcut service failed"))
        finally:
            self._release()
            with self._lock:
                # Under the lock, so _wake never writes to a reused descriptor.
                self._pipe_open = False
                os.close(self._wake_read)
                os.close(self._wake_write)

    def _open(self) -> bool:
        lib = _load_xlib()
        if lib is None:
            self._fail(focus_only("The X11 library was not found"))
            return False
        self._lib = lib
        display = lib.XOpenDisplay(None)
        if not display:
            self._fail(focus_only("Could not connect to the X display"))
            return False
        self._display = display
        self._root = lib.XDefaultRootWindow(display)
        # Held keys then repeat as presses only, so a repeat is recognisable.
        lib.XkbSetDetectableAutoRepeat(display, 1, None)
        for kind, keysym in _KEYSYMS.items():
            keycode = lib.XKeysymToKeycode(display, keysym)
            if keycode:
                self._keycodes[keycode] = kind
        if len(self._keycodes) != len(_KEYSYMS):
            self._fail(focus_only("The keyboard layout has no P or F key"))
            return False
        return True

    def _fail(self, status: HotkeyStatus) -> None:
        """A failure for every later enable; the thread ends after it."""
        with self._lock:
            self._failure = status
            if self._wanted:
                self._status = status
            self._applied_generation = self._generation
            self._applied.notify_all()

    def _loop(self) -> None:
        lib, display = self._lib, self._display
        assert lib is not None and display is not None
        connection = lib.XConnectionNumber(display)
        event = _XEvent()
        while True:
            with self._lock:
                wanted, closed, generation = (
                    self._wanted,
                    self._closed,
                    self._generation,
                )
            if closed:
                return
            self._reconcile(wanted, generation)
            while lib.XPending(display) > 0:
                lib.XNextEvent(display, ctypes.byref(event))
                self._on_event(event)
            ready, _, _ = select.select([connection, self._wake_read], [], [])
            if self._wake_read in ready:
                os.read(self._wake_read, 4096)

    def _reconcile(self, wanted: bool, generation: int) -> None:
        if wanted and not self._grabbed and generation != self._attempted:
            self._attempted = generation
            status = self._grab()
            if self._dry_run and self._grabbed:
                self._ungrab()
            self._publish(status, generation)
        elif not wanted and self._grabbed:
            self._ungrab()
            self._publish(OFF_STATUS, generation)
        elif wanted and self._grabbed:
            # A disable set OFF eagerly, then an enable came before this
            # thread released the grab: it is still held, so say so.
            self._publish(HotkeyStatus(state=HotkeyState.ACTIVE), generation)
        else:
            self._publish(None, generation)

    def _publish(self, status: HotkeyStatus | None, generation: int) -> None:
        with self._lock:
            if status is not None and generation == self._generation:
                self._status = status
            self._applied_generation = max(self._applied_generation, generation)
            self._applied.notify_all()

    def _grab(self) -> HotkeyStatus:
        lib, display = self._lib, self._display
        assert lib is not None and display is not None
        refused: _GrabRefusedError | None = None
        self._trap_errors()
        try:
            for keycode, kind in self._keycodes.items():
                self._grab_one(keycode, kind)
        except _GrabRefusedError as exc:
            refused = exc
        finally:
            self._untrap_errors()
        if refused is not None:
            self._ungrab()
            return focus_only(str(refused))
        self._grabbed = True
        if self._dry_run:
            return HotkeyStatus(
                state=HotkeyState.ACTIVE,
                reason="The shortcuts are free (not kept in a dry run)",
            )
        return HotkeyStatus(state=HotkeyState.ACTIVE)

    def _grab_one(self, keycode: int, kind: HotkeyKind) -> None:
        """Grab one key; the caller has the error trap installed."""
        lib, display = self._lib, self._display
        assert lib is not None and display is not None
        self._errors.clear()
        for modifiers in _MODIFIER_SETS:
            lib.XGrabKey(
                display,
                keycode,
                modifiers,
                self._root,
                0,
                _GRAB_MODE_ASYNC,
                _GRAB_MODE_ASYNC,
            )
        lib.XSync(display, 0)
        if self._errors:
            logger.info("X11 grab of %s refused: %s", kind.value, self._errors)
            if _BAD_ACCESS in self._errors:
                raise _GrabRefusedError(f"{_LABELS[kind]} is used by another app")
            raise _GrabRefusedError(f"X11 refused {_LABELS[kind]}")

    def _ungrab(self) -> None:
        lib, display = self._lib, self._display
        self._grabbed = False
        self._down.clear()
        if lib is None or display is None:
            return
        self._errors.clear()
        self._trap_errors()
        try:
            for keycode in self._keycodes:
                for modifiers in _MODIFIER_SETS:
                    lib.XUngrabKey(display, keycode, modifiers, self._root)
            lib.XSync(display, 0)
        finally:
            self._untrap_errors()

    def _trap_errors(self) -> None:
        """Install the process-wide error handler; pair with _untrap_errors.

        Keep the span short (only requests and their XSync): Tk keeps
        running meanwhile and its errors pass through ``_on_error`` to the
        handler it installed.
        """
        lib = self._lib
        assert lib is not None
        self._previous_handler = lib.XSetErrorHandler(self._handler)

    def _untrap_errors(self) -> None:
        lib = self._lib
        assert lib is not None
        # _previous_handler stays set: a Tk-thread error already inside
        # _on_error must still find it.
        lib.XSetErrorHandler(self._previous_handler)

    def _on_error(
        self, display: int | None, error: "ctypes._Pointer[_XErrorEvent]"
    ) -> int:
        # Runs on whichever thread hit the error; never raises into Xlib.
        # Errors of other connections (Tk's) go to the handler installed
        # before ours, so Tk's own error handling keeps working.
        if display != self._display:
            previous = self._previous_handler
            if isinstance(previous, _ERROR_HANDLER) and previous:
                return int(previous(display, error))
            return 0
        self._errors.append(int(error.contents.error_code))
        return 0

    def _on_event(self, event: _XEvent) -> None:
        if event.type == _KEY_RELEASE:
            self._down.discard(event.xkey.keycode)
            return
        if event.type != _KEY_PRESS or not self._grabbed:
            return
        keycode = event.xkey.keycode
        kind = self._keycodes.get(keycode)
        if kind is None or keycode in self._down:
            return
        self._down.add(keycode)
        with self._lock:
            deliver = self._wanted and not self._closed
        if deliver:
            self._events.put(kind)

    def _release(self) -> None:
        lib, display = self._lib, self._display
        if lib is None or display is None:
            return
        if self._grabbed:
            self._ungrab()
        self._display = None
        lib.XCloseDisplay(display)
