"""ctypes bindings for libX11, libXcomposite and libXrandr (window capture).

Used by
``window_inventory_x11``, ``window_grab_x11`` and ``x11_desktop_switch``.

* Importing this module does no I/O: the libraries are looked up
  (``ctypes.util.find_library``) and loaded on the first ``open_display``.
* Each call runs on its own ``Display`` connection, opened and closed in the
  calling (worker) thread by ``open_display``; nothing X related is shared
  between threads except the loaded libraries and the error handler.
* Xlib's default error handler prints and exits the process. A window can
  vanish between two requests (BadWindow), so one process-wide handler is
  installed when libX11 is first loaded and kept for the process lifetime: it
  records the errors of the displays this module opened and hands every
  other display's errors to the handler installed before it, so other Xlib
  users keep their behaviour. Methods that may hit an error return None
  instead of raising.
* A lost X connection (the X server quit, the session ended) is an X *IO*
  error, whose default handling exits the process. With libX11 1.7 or later
  every display this module opens gets its own exit handler
  (``XSetIOErrorExitHandler``) that only marks the display lost; the next
  check raises ``CaptureUnavailableError(capture_failed)`` and the display is
  dropped without ``XCloseDisplay``. Older libX11 has no per-display exit
  handler, and a process-wide ``XSetIOErrorHandler`` cannot stop the exit
  (Xlib exits when that handler returns, and Python cannot ``longjmp`` out
  of it), so there a lost X connection still ends the runner; a warning is
  logged once when the library is loaded.
* ``manual_hotkeys_x11.prepare_x11_threads`` (``XInitThreads``, idempotent)
  runs before the first Xlib call of this module.
* libXcomposite (window-only images) and libXrandr (monitor rects) are
  optional: without them ``composite_available`` is False and ``monitors``
  falls back to the whole root window.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from PIL import Image

from controller_client.manual_hotkeys_x11 import prepare_x11_threads
from controller_client.protocol import CaptureStatus
from controller_client.window_model import CaptureUnavailableError, Rect

logger = logging.getLogger(__name__)

# Xlib constants.
_ANY_PROPERTY_TYPE: Final = 0
_SUCCESS: Final = 0
_Z_PIXMAP: Final = 2
_LSB_FIRST: Final = 0
_CLIENT_MESSAGE: Final = 33
_SUBSTRUCTURE_NOTIFY_MASK: Final = 1 << 19
_SUBSTRUCTURE_REDIRECT_MASK: Final = 1 << 20
_ALL_PLANES: Final = ctypes.c_ulong(-1).value
# map_state of XWindowAttributes.
MAP_STATE_VIEWABLE: Final = 2
# class of XWindowAttributes.
_INPUT_ONLY: Final = 2
# Longest property read, in 32-bit units (1 MiB): bounds a hostile client's
# property, far above any real client list, title or desktop name list.
_MAX_PROPERTY_LONGS: Final = 256 * 1024
# A 32-bit CARDINAL as Xlib returns it (in a C long); the all-desktops value.
CARDINAL_MASK: Final = 0xFFFFFFFF

_LIBX11_DETAIL: Final = (
    "the X11 library (libX11) was not found on the test machine; install it"
)
_CONNECTION_LOST_DETAIL: Final = "the connection to the X display was lost"
_DISPLAY_DETAIL: Final = (
    "could not connect to the X display; start the Test Runner inside the "
    "logged-in desktop session"
)
EWMH_DETAIL: Final = (
    "the window manager does not support the EWMH standard; log in to a "
    "GNOME, Plasma, Xfce or Cinnamon session"
)


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
# void (*XIOErrorExitHandler)(Display *, void *user_data), libX11 1.7+.
_IO_EXIT_HANDLER = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("c_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
        # struct funcs: six function pointers.
        ("f", ctypes.c_void_p * 6),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xclient", _XClientMessageEvent),
        ("pad", ctypes.c_long * 24),
    ]


class _XRRMonitorInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_ulong),
        ("primary", ctypes.c_int),
        ("automatic", ctypes.c_int),
        ("noutput", ctypes.c_int),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("mwidth", ctypes.c_int),
        ("mheight", ctypes.c_int),
        ("outputs", ctypes.c_void_p),
    ]


@dataclass(frozen=True)
class WindowAttributes:
    """The parts of ``XWindowAttributes`` the capture uses (x / y relative
    to the parent)."""

    x: int
    y: int
    width: int
    height: int
    border_width: int
    depth: int
    map_state: int
    override_redirect: bool = False
    input_only: bool = False

    @property
    def viewable(self) -> bool:
        return self.map_state == MAP_STATE_VIEWABLE


@dataclass(frozen=True)
class Monitor:
    """One monitor in root window pixels."""

    rect: Rect
    primary: bool


@dataclass(frozen=True)
class _Libraries:
    x11: ctypes.CDLL
    composite: ctypes.CDLL | None
    xrandr: ctypes.CDLL | None
    # libX11 1.7+: XSetIOErrorExitHandler is available.
    io_exit_handler: bool


_load_lock = threading.Lock()
_loaded: _Libraries | None = None
_load_failed = False
# Errors of the displays this module opened, by Display pointer.
_errors_lock = threading.Lock()
_errors: dict[int, list[int]] = {}
# Displays of this module whose connection was lost (IO error).
_lost: set[int] = set()
_previous_handler: object = None


def _on_error(display: int | None, error: ctypes._Pointer[_XErrorEvent]) -> int:
    # Runs on whichever thread hit the error; never raises into Xlib.
    with _errors_lock:
        recorded = _errors.get(display) if display is not None else None
        if recorded is not None:
            recorded.append(int(error.contents.error_code))
            return 0
        previous = _previous_handler
    if isinstance(previous, _ERROR_HANDLER) and previous:
        return int(previous(display, error))
    return 0


# Referenced for the process lifetime (ctypes would free it).
_HANDLER: Final = _ERROR_HANDLER(_on_error)


def _on_io_exit(display: int | None, _user_data: int | None) -> None:
    # Called by Xlib instead of exit() when a display's connection is lost;
    # returning leaves the display marked dead (every later call fails fast).
    if display is not None:
        with _errors_lock:
            _lost.add(display)


_IO_EXIT: Final = _IO_EXIT_HANDLER(_on_io_exit)


def _open_library(name: str) -> ctypes.CDLL | None:
    path = ctypes.util.find_library(name)
    if path is None:
        return None
    try:
        return ctypes.CDLL(path)
    except OSError:
        return None


def _configure_x11(lib: ctypes.CDLL) -> None:
    vp, ul, ci, cu = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = vp
    lib.XCloseDisplay.argtypes = [vp]
    lib.XCloseDisplay.restype = ci
    lib.XDefaultRootWindow.argtypes = [vp]
    lib.XDefaultRootWindow.restype = ul
    lib.XDefaultScreen.argtypes = [vp]
    lib.XDefaultScreen.restype = ci
    lib.XInternAtom.argtypes = [vp, ctypes.c_char_p, ci]
    lib.XInternAtom.restype = ul
    lib.XGetWindowProperty.argtypes = [
        vp,
        ul,
        ul,
        ctypes.c_long,
        ctypes.c_long,
        ci,
        ul,
        ctypes.POINTER(ul),
        ctypes.POINTER(ci),
        ctypes.POINTER(ul),
        ctypes.POINTER(ul),
        ctypes.POINTER(vp),
    ]
    lib.XGetWindowProperty.restype = ci
    lib.XFree.argtypes = [vp]
    lib.XFree.restype = ci
    lib.XGetWindowAttributes.argtypes = [
        vp,
        ul,
        ctypes.POINTER(_XWindowAttributes),
    ]
    lib.XGetWindowAttributes.restype = ci
    lib.XTranslateCoordinates.argtypes = [
        vp,
        ul,
        ul,
        ci,
        ci,
        ctypes.POINTER(ci),
        ctypes.POINTER(ci),
        ctypes.POINTER(ul),
    ]
    lib.XTranslateCoordinates.restype = ci
    lib.XQueryTree.argtypes = [
        vp,
        ul,
        ctypes.POINTER(ul),
        ctypes.POINTER(ul),
        ctypes.POINTER(vp),
        ctypes.POINTER(cu),
    ]
    lib.XQueryTree.restype = ci
    lib.XGetImage.argtypes = [vp, ul, ci, ci, cu, cu, ul, ci]
    lib.XGetImage.restype = ctypes.POINTER(_XImage)
    lib.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
    lib.XDestroyImage.restype = ci
    lib.XGetSelectionOwner.argtypes = [vp, ul]
    lib.XGetSelectionOwner.restype = ul
    lib.XSendEvent.argtypes = [vp, ul, ci, ctypes.c_long, ctypes.POINTER(_XEvent)]
    lib.XSendEvent.restype = ci
    lib.XSync.argtypes = [vp, ci]
    lib.XSync.restype = ci
    lib.XFreePixmap.argtypes = [vp, ul]
    lib.XFreePixmap.restype = ci
    lib.XSetErrorHandler.argtypes = [_ERROR_HANDLER]
    lib.XSetErrorHandler.restype = _ERROR_HANDLER


def _configure_io_exit_handler(lib: ctypes.CDLL) -> bool:
    """Whether libX11 has ``XSetIOErrorExitHandler`` (1.7+)."""
    try:
        lib.XSetIOErrorExitHandler.argtypes = [
            ctypes.c_void_p,
            _IO_EXIT_HANDLER,
            ctypes.c_void_p,
        ]
        lib.XSetIOErrorExitHandler.restype = None
    except AttributeError:
        return False
    return True


def _configure_composite(lib: ctypes.CDLL) -> bool:
    try:
        lib.XCompositeQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.XCompositeQueryExtension.restype = ctypes.c_int
        lib.XCompositeNameWindowPixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XCompositeNameWindowPixmap.restype = ctypes.c_ulong
    except AttributeError:
        return False
    return True


def _configure_xrandr(lib: ctypes.CDLL) -> bool:
    try:
        lib.XRRGetMonitors.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.XRRGetMonitors.restype = ctypes.POINTER(_XRRMonitorInfo)
        lib.XRRFreeMonitors.argtypes = [ctypes.POINTER(_XRRMonitorInfo)]
        lib.XRRFreeMonitors.restype = None
    except AttributeError:
        # libXrandr older than 1.5 (no RandR monitors).
        return False
    return True


def _libraries() -> _Libraries | None:
    """Load and configure the libraries once; None without libX11."""
    global _loaded, _load_failed, _previous_handler
    with _load_lock:
        if _loaded is not None or _load_failed:
            return _loaded
        prepare_x11_threads()
        x11 = _open_library("X11")
        if x11 is None:
            _load_failed = True
            logger.warning("libX11 was not found; X11 window capture is off")
            return None
        _configure_x11(x11)
        io_exit_handler = _configure_io_exit_handler(x11)
        if not io_exit_handler:
            logger.warning(
                "libX11 is older than 1.7: a lost X connection during a "
                "capture ends the Test Runner"
            )
        composite = _open_library("Xcomposite")
        if composite is not None and not _configure_composite(composite):
            composite = None
        if composite is None:
            logger.info("libXcomposite was not found; no window-only X11 images")
        xrandr = _open_library("Xrandr")
        if xrandr is not None and not _configure_xrandr(xrandr):
            xrandr = None
        with _errors_lock:
            _previous_handler = x11.XSetErrorHandler(_HANDLER)
        _loaded = _Libraries(
            x11=x11,
            composite=composite,
            xrandr=xrandr,
            io_exit_handler=io_exit_handler,
        )
        return _loaded


def _decode_utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class X11Display:
    """One open ``Display`` (created by ``open_display`` only)."""

    def __init__(self, libs: _Libraries, handle: int) -> None:
        self._libs = libs
        self._x = libs.x11
        self._handle = handle
        self._atoms: dict[str, int] = {}
        self.root: int = int(self._x.XDefaultRootWindow(handle))
        self.screen: int = int(self._x.XDefaultScreen(handle))

    # -- errors -----------------------------------------------------------

    def _ensure_alive(self) -> None:
        """Raise ``capture_failed`` once the connection is lost."""
        with _errors_lock:
            lost = self._handle in _lost
        if lost:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, _CONNECTION_LOST_DETAIL
            )

    def _clear_errors(self) -> None:
        self._ensure_alive()
        with _errors_lock:
            recorded = _errors.get(self._handle)
            if recorded is not None:
                recorded.clear()

    def _take_errors(self) -> list[int]:
        self._ensure_alive()
        with _errors_lock:
            recorded = _errors.get(self._handle)
            if recorded is None:
                return []
            taken = list(recorded)
            recorded.clear()
            return taken

    # -- atoms and properties ---------------------------------------------

    def atom(self, name: str) -> int:
        """The atom for ``name`` (created when missing; cached per display)."""
        cached = self._atoms.get(name)
        if cached is not None:
            return cached
        self._ensure_alive()
        value = int(self._x.XInternAtom(self._handle, name.encode("ascii"), 0))
        self._ensure_alive()
        self._atoms[name] = value
        return value

    def _property(
        self, window: int, name: str, req_type: int = _ANY_PROPERTY_TYPE
    ) -> tuple[int, int, int, int] | None:
        """(actual_type, format, nitems, data pointer) or None when unset.

        The caller frees the pointer with ``XFree`` (``_free``)."""
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.c_void_p()
        self._clear_errors()
        status = self._x.XGetWindowProperty(
            self._handle,
            window,
            self.atom(name),
            0,
            _MAX_PROPERTY_LONGS,
            0,
            req_type,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(nitems),
            ctypes.byref(bytes_after),
            ctypes.byref(data),
        )
        errors = self._take_errors()
        if status != _SUCCESS or errors or not data.value:
            if data.value:
                self._x.XFree(data)
            return None
        if actual_type.value == 0 or (
            req_type != _ANY_PROPERTY_TYPE and actual_type.value != req_type
        ):
            # Unset, or of another type than asked for.
            self._x.XFree(data)
            return None
        return actual_type.value, actual_format.value, nitems.value, data.value

    def _free(self, pointer: int) -> None:
        self._x.XFree(ctypes.c_void_p(pointer))

    def cardinals(self, window: int, name: str) -> list[int] | None:
        """A 32-bit list property (CARDINAL, WINDOW, ATOM) as unsigned 32-bit
        values; None when unset or of another format."""
        found = self._property(window, name)
        if found is None:
            return None
        _, fmt, nitems, pointer = found
        try:
            if fmt != 32:
                return None
            # Format 32 items are C longs in Xlib's client memory.
            values = (ctypes.c_ulong * nitems).from_address(pointer)
            return [int(value) & CARDINAL_MASK for value in values]
        finally:
            self._free(pointer)

    def cardinal(self, window: int, name: str) -> int | None:
        values = self.cardinals(window, name)
        return values[0] if values else None

    def _bytes(self, window: int, name: str, req_type: int) -> bytes | None:
        found = self._property(window, name, req_type)
        if found is None:
            return None
        _, fmt, nitems, pointer = found
        try:
            if fmt != 8:
                return None
            return ctypes.string_at(pointer, nitems)
        finally:
            self._free(pointer)

    def utf8_text(self, window: int, name: str) -> str | None:
        """A UTF8_STRING property (``_NET_WM_NAME``)."""
        data = self._bytes(window, name, self.atom("UTF8_STRING"))
        if data is None:
            return None
        return _decode_utf8(data.split(b"\0", 1)[0])

    def legacy_text(self, window: int, name: str) -> str | None:
        """A STRING / COMPOUND_TEXT property (``WM_NAME``), best effort."""
        data = self._bytes(window, name, _ANY_PROPERTY_TYPE)
        if data is None:
            return None
        data = data.split(b"\0", 1)[0]
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    def utf8_list(self, window: int, name: str) -> list[str]:
        """A NUL-separated UTF8_STRING list (``_NET_DESKTOP_NAMES``)."""
        data = self._bytes(window, name, self.atom("UTF8_STRING"))
        if not data:
            return []
        parts = data.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return [_decode_utf8(part) for part in parts]

    def atom_names_present(
        self, window: int, name: str, wanted: Sequence[str]
    ) -> set[str]:
        """Which of ``wanted`` atoms are in the ATOM list property ``name``."""
        values = self.cardinals(window, name)
        if not values:
            return set()
        present = set(values)
        return {atom for atom in wanted if self.atom(atom) in present}

    # -- geometry ---------------------------------------------------------

    def attributes(self, window: int) -> WindowAttributes | None:
        attrs = _XWindowAttributes()
        self._clear_errors()
        ok = self._x.XGetWindowAttributes(self._handle, window, ctypes.byref(attrs))
        if not ok or self._take_errors():
            return None
        return WindowAttributes(
            x=attrs.x,
            y=attrs.y,
            width=attrs.width,
            height=attrs.height,
            border_width=attrs.border_width,
            depth=attrs.depth,
            map_state=attrs.map_state,
            override_redirect=bool(attrs.override_redirect),
            input_only=attrs.c_class == _INPUT_ONLY,
        )

    def root_position(self, window: int) -> tuple[int, int] | None:
        """Where ``window``'s (0, 0) is in root coordinates."""
        x = ctypes.c_int()
        y = ctypes.c_int()
        child = ctypes.c_ulong()
        self._clear_errors()
        ok = self._x.XTranslateCoordinates(
            self._handle,
            window,
            self.root,
            0,
            0,
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(child),
        )
        if not ok or self._take_errors():
            return None
        return x.value, y.value

    def root_children(self) -> list[int] | None:
        """The root window's children in stacking order, bottom to top
        (frames of managed windows and override-redirect windows); None when
        the tree cannot be read."""
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        children = ctypes.c_void_p()
        count = ctypes.c_uint()
        self._clear_errors()
        ok = self._x.XQueryTree(
            self._handle,
            self.root,
            ctypes.byref(root),
            ctypes.byref(parent),
            ctypes.byref(children),
            ctypes.byref(count),
        )
        try:
            if not ok or self._take_errors():
                return None
            if not children.value or count.value == 0:
                return []
            values = (ctypes.c_ulong * count.value).from_address(children.value)
            return [int(value) for value in values]
        finally:
            if children.value:
                self._x.XFree(children)

    def parent(self, window: int) -> int | None:
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        children = ctypes.c_void_p()
        count = ctypes.c_uint()
        self._clear_errors()
        ok = self._x.XQueryTree(
            self._handle,
            window,
            ctypes.byref(root),
            ctypes.byref(parent),
            ctypes.byref(children),
            ctypes.byref(count),
        )
        if children.value:
            self._x.XFree(children)
        if not ok or self._take_errors():
            return None
        return parent.value

    def top_level(self, window: int) -> int | None:
        """The child of the root that holds ``window`` (its frame when the
        window manager reparents; ``window`` itself otherwise)."""
        current = window
        # Real trees are a few levels deep; the bound stops a cycle.
        for _ in range(64):
            parent = self.parent(current)
            if parent is None or parent == 0:
                return None
            if parent == self.root:
                return current
            current = parent
        return None

    def root_rect(self) -> Rect:
        attrs = self.attributes(self.root)
        if attrs is None:
            raise CaptureUnavailableError(
                CaptureStatus.CAPTURE_FAILED, "the X screen size could not be read"
            )
        return Rect(0, 0, attrs.width, attrs.height)

    def monitors(self) -> list[Monitor]:
        """The active monitors (RandR 1.5); the whole root when unknown."""
        xrandr = self._libs.xrandr
        if xrandr is not None:
            count = ctypes.c_int()
            self._clear_errors()
            infos = xrandr.XRRGetMonitors(
                self._handle, self.root, 1, ctypes.byref(count)
            )
            errors = self._take_errors()
            if infos:
                try:
                    found = [
                        Monitor(
                            Rect(info.x, info.y, info.width, info.height),
                            bool(info.primary),
                        )
                        for info in infos[: count.value]
                        if info.width > 0 and info.height > 0
                    ]
                finally:
                    xrandr.XRRFreeMonitors(infos)
                if found and not errors:
                    return found
        return [Monitor(self.root_rect(), True)]

    # -- images -----------------------------------------------------------

    def get_image(
        self, drawable: int, x: int, y: int, width: int, height: int
    ) -> Image.Image | None:
        """``XGetImage`` (ZPixmap) of ``drawable`` as an RGB image; None when
        the server refused (BadMatch outside the drawable, BadDrawable) or the
        pixel layout is not a 32-bit true colour one."""
        if width <= 0 or height <= 0:
            return None
        self._clear_errors()
        image = self._x.XGetImage(
            self._handle, drawable, x, y, width, height, _ALL_PLANES, _Z_PIXMAP
        )
        errors = self._take_errors()
        if not image:
            return None
        try:
            if errors:
                return None
            info = image.contents
            raw_mode = _raw_mode(info)
            if raw_mode is None or not info.data:
                logger.warning(
                    "Unsupported X image layout: %d bpp, depth %d",
                    info.bits_per_pixel,
                    info.depth,
                )
                return None
            data = ctypes.string_at(info.data, info.bytes_per_line * info.height)
            return Image.frombytes(
                "RGB",
                (info.width, info.height),
                data,
                "raw",
                raw_mode,
                info.bytes_per_line,
            )
        finally:
            self._x.XDestroyImage(image)

    # -- selections, messages ---------------------------------------------

    def selection_owner(self, name: str) -> int:
        atom = self.atom(name)
        owner = int(self._x.XGetSelectionOwner(self._handle, atom))
        self._ensure_alive()
        return owner

    def send_root_message(
        self, window: int, message_type: str, data: Sequence[int]
    ) -> bool:
        """An EWMH client message about ``window`` to the root window
        (SubstructureRedirect | SubstructureNotify); True when sent without
        an X error."""
        event = _XEvent()
        event.xclient.type = _CLIENT_MESSAGE
        event.xclient.send_event = 1
        event.xclient.display = self._handle
        event.xclient.window = window
        event.xclient.message_type = self.atom(message_type)
        event.xclient.format = 32
        for index, value in enumerate(data[:5]):
            event.xclient.data[index] = value
        self._clear_errors()
        ok = self._x.XSendEvent(
            self._handle,
            self.root,
            0,
            _SUBSTRUCTURE_REDIRECT_MASK | _SUBSTRUCTURE_NOTIFY_MASK,
            ctypes.byref(event),
        )
        self._x.XSync(self._handle, 0)
        return bool(ok) and not self._take_errors()

    # -- composite --------------------------------------------------------

    def composite_available(self) -> bool:
        composite = self._libs.composite
        if composite is None:
            return False
        event_base = ctypes.c_int()
        error_base = ctypes.c_int()
        return bool(
            composite.XCompositeQueryExtension(
                self._handle, ctypes.byref(event_base), ctypes.byref(error_base)
            )
        )

    def name_window_pixmap(self, window: int) -> int | None:
        """The redirected window's off-screen pixmap (free it with
        ``free_pixmap``); None without the extension or when the window is
        not redirected (BadMatch)."""
        composite = self._libs.composite
        if composite is None:
            return None
        self._clear_errors()
        pixmap = int(composite.XCompositeNameWindowPixmap(self._handle, window))
        self._x.XSync(self._handle, 0)
        if self._take_errors() or pixmap == 0:
            if pixmap:
                self.free_pixmap(pixmap)
            return None
        return pixmap

    def free_pixmap(self, pixmap: int) -> None:
        self._clear_errors()
        self._x.XFreePixmap(self._handle, pixmap)
        self._x.XSync(self._handle, 0)
        self._take_errors()

    # -- EWMH -------------------------------------------------------------

    def require_ewmh(self) -> None:
        """Raise ``compositor_unsupported`` unless an EWMH window manager
        runs (``_NET_SUPPORTING_WM_CHECK`` on the root and its child)."""
        check = self.cardinal(self.root, "_NET_SUPPORTING_WM_CHECK")
        if not check or self.cardinal(check, "_NET_SUPPORTING_WM_CHECK") != check:
            raise CaptureUnavailableError(
                CaptureStatus.COMPOSITOR_UNSUPPORTED, EWMH_DETAIL
            )


def _raw_mode(info: _XImage) -> str | None:
    """Pillow's raw mode for a 32-bit true colour ZPixmap, else None."""
    if info.format != _Z_PIXMAP or info.bits_per_pixel != 32:
        return None
    red = int(info.red_mask)
    blue = int(info.blue_mask)
    if info.byte_order == _LSB_FIRST:
        if red == 0xFF0000 and blue == 0xFF:
            return "BGRX"
        if red == 0xFF and blue == 0xFF0000:
            return "RGBX"
        return None
    if red == 0xFF0000 and blue == 0xFF:
        return "XRGB"
    if red == 0xFF and blue == 0xFF0000:
        return "XBGR"
    return None


@contextmanager
def open_display() -> Iterator[X11Display]:
    """Open the ``DISPLAY`` connection for one call and always close it.

    Raises ``CaptureUnavailableError``: ``helper_missing`` without libX11,
    ``not_interactive`` when the display cannot be opened.
    """
    libs = _libraries()
    if libs is None:
        raise CaptureUnavailableError(CaptureStatus.HELPER_MISSING, _LIBX11_DETAIL)
    handle = libs.x11.XOpenDisplay(None)
    if not handle:
        raise CaptureUnavailableError(CaptureStatus.NOT_INTERACTIVE, _DISPLAY_DETAIL)
    handle = int(handle)
    with _errors_lock:
        _errors[handle] = []
    if libs.io_exit_handler:
        libs.x11.XSetIOErrorExitHandler(handle, _IO_EXIT, None)
    try:
        yield X11Display(libs, handle)
    finally:
        with _errors_lock:
            lost = handle in _lost
            _lost.discard(handle)
        if not lost:
            # A lost display is left as it is: closing a dead connection
            # would only hit the IO error again.
            libs.x11.XCloseDisplay(handle)
        with _errors_lock:
            _errors.pop(handle, None)
