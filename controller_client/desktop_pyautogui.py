"""The Windows and macOS desktop backend, built on pyautogui.

Imported only when ``sys.platform != "linux"`` (see ``desktop.py``); Linux
uses the GNOME Wayland backend instead.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Sequence
from typing import Final

import pyautogui
from PIL import Image

from controller_client.desktop_backend import DesktopBackend
from controller_client.diagnostics import DiagnosticStep
from controller_client.exceptions import ExecutionError, ScreenCaptureError
from controller_client.protocol import ScrollDirection

if sys.platform == "darwin":
    import Quartz

    from controller_client.screen_capture_macos import capture_screen_macos
elif sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

try:
    from screeninfo import get_monitors

    _SCREENINFO_AVAILABLE = True
except ImportError:
    _SCREENINFO_AVAILABLE = False

logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = False

_SCREEN_CAPTURE_HINT: Final = (
    "The Test Runner must run inside an interactive desktop session that may "
    "capture the screen: on macOS grant Screen Recording to the terminal, on "
    "Windows run it from a logged-in desktop session rather than a service."
)

# pyautogui silently skips key names its platform backend does not know, so
# "cmd+n" on macOS (which only knows "command") pressed a bare "n". Map the
# common aliases onto each backend's own names and reject anything unknown.
if sys.platform == "darwin":
    _KEY_ALIASES: Final[dict[str, str]] = {
        "cmd": "command",
        "meta": "command",
        "super": "command",
        "win": "command",
        "opt": "option",
        "control": "ctrl",
    }
else:
    _KEY_ALIASES = {
        "cmd": "win",
        "command": "win",
        "meta": "win",
        "super": "win",
        "opt": "alt",
        "option": "alt",
        "control": "ctrl",
    }


if sys.platform == "darwin":

    def _double_click(x: int, y: int) -> None:
        # pyautogui 0.9.54 posts two plain down/up pairs on macOS and never sets
        # kCGMouseEventClickState, the field AppKit reads as clickCount, so Cocoa
        # apps saw two single clicks. Post the pairs with click state 1 then 2.
        pyautogui.moveTo(x=x, y=y)
        point = Quartz.CGPointMake(x, y)
        for click_state in (1, 2):
            for event_type in (
                Quartz.kCGEventLeftMouseDown,
                Quartz.kCGEventLeftMouseUp,
            ):
                event = Quartz.CGEventCreateMouseEvent(
                    None, event_type, point, Quartz.kCGMouseButtonLeft
                )
                if event is None:
                    raise ExecutionError("Could not create a mouse event")
                Quartz.CGEventSetIntegerValueField(
                    event, Quartz.kCGMouseEventClickState, click_state
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _capture_screen() -> Image.Image:
        return capture_screen_macos()

else:

    def _double_click(x: int, y: int) -> None:
        # Windows detects the double-click from the timing of the two clicks
        # pyautogui sends back to back.
        pyautogui.doubleClick(x=x, y=y)

    def _capture_screen() -> Image.Image:
        try:
            return pyautogui.screenshot()
        except Exception as exc:
            raise ScreenCaptureError(f"screen capture failed ({exc})") from exc


# pyautogui 0.9.54's scroll is wrong on both platforms (Windows sends 1/120 of
# a notch and turns horizontal into vertical; macOS scrolls left partly
# right), so the backend posts the wheel events itself. One notch is one wheel
# click; the executor has already mapped x/y into input space.
if sys.platform == "darwin":
    # Lines per notch, and (dy, dx) per direction for
    # CGEventCreateScrollWheelEvent: wheel 1 is vertical, wheel 2 horizontal.
    _MAC_LINES_PER_NOTCH: Final = 3
    _MAC_SCROLL_DELTAS: Final[dict[ScrollDirection, tuple[int, int]]] = {
        ScrollDirection.UP: (_MAC_LINES_PER_NOTCH, 0),
        ScrollDirection.DOWN: (-_MAC_LINES_PER_NOTCH, 0),
        ScrollDirection.LEFT: (0, _MAC_LINES_PER_NOTCH),
        ScrollDirection.RIGHT: (0, -_MAC_LINES_PER_NOTCH),
    }
    _MAC_MODIFIER_MASK: Final = (
        Quartz.kCGEventFlagMaskShift
        | Quartz.kCGEventFlagMaskControl
        | Quartz.kCGEventFlagMaskAlternate
        | Quartz.kCGEventFlagMaskCommand
    )
    # Keys to release per modifier flag. A lone modifier key-up is only a
    # flagsChanged event on macOS, so releasing a side that is not down is safe.
    _MAC_MODIFIER_KEYS: Final = (
        (Quartz.kCGEventFlagMaskShift, ("shift", "shiftright")),
        (Quartz.kCGEventFlagMaskControl, ("ctrl", "ctrlright")),
        (Quartz.kCGEventFlagMaskAlternate, ("option", "optionright")),
        (Quartz.kCGEventFlagMaskCommand, ("command",)),
    )
    _MODIFIER_SETTLE_SECONDS: Final = 0.02

    def _scroll(direction: ScrollDirection, amount: int) -> None:
        dy, dx = _MAC_SCROLL_DELTAS[direction]
        for _ in range(amount):
            event = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitLine, 2, dy, dx
            )
            if event is None:
                raise ExecutionError("Could not create a scroll wheel event")
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _release_modifiers(context: str) -> None:
        # Typing while Ctrl+Cmd is still down turns a space into Ctrl+Cmd+Space
        # (the emoji picker). Release only what is still set: every key event
        # costs pyautogui's 10 ms catch-up sleep on macOS.
        flags = Quartz.CGEventSourceFlagsState(
            Quartz.kCGEventSourceStateCombinedSessionState
        )
        if not flags & _MAC_MODIFIER_MASK:
            return
        logger.warning(
            "Modifier flags still set %s: 0x%x", context, flags & _MAC_MODIFIER_MASK
        )
        for mask, keys in _MAC_MODIFIER_KEYS:
            if flags & mask:
                for key in keys:
                    pyautogui.keyUp(key, _pause=False)
        time.sleep(_MODIFIER_SETTLE_SECONDS)

    def _dpi_awareness_note() -> str:
        return ""

elif sys.platform == "win32":
    _MOUSEEVENTF_WHEEL: Final = 0x0800
    _MOUSEEVENTF_HWHEEL: Final = 0x01000
    _WHEEL_DELTA: Final = 120
    # Up and right are positive wheel deltas on Windows.
    _WIN_SCROLL_EVENTS: Final[dict[ScrollDirection, tuple[int, int]]] = {
        ScrollDirection.UP: (_MOUSEEVENTF_WHEEL, 1),
        ScrollDirection.DOWN: (_MOUSEEVENTF_WHEEL, -1),
        ScrollDirection.LEFT: (_MOUSEEVENTF_HWHEEL, -1),
        ScrollDirection.RIGHT: (_MOUSEEVENTF_HWHEEL, 1),
    }
    # Left/right virtual-key codes and the pyautogui names that release them.
    _WIN_MODIFIER_KEYS: Final[dict[int, str]] = {
        0xA0: "shiftleft",  # VK_LSHIFT
        0xA1: "shiftright",  # VK_RSHIFT
        0xA2: "ctrlleft",  # VK_LCONTROL
        0xA3: "ctrlright",  # VK_RCONTROL
        0xA4: "altleft",  # VK_LMENU
        0xA5: "altright",  # VK_RMENU
        0x5B: "winleft",  # VK_LWIN
        0x5C: "winright",  # VK_RWIN
    }
    _KEY_DOWN_BIT: Final = 0x8000

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_long,  # dwData: signed wheel delta
        ctypes.c_size_t,  # dwExtraInfo: ULONG_PTR
    ]
    _user32.mouse_event.restype = None
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.IsProcessDPIAware.argtypes = []
    _user32.IsProcessDPIAware.restype = wintypes.BOOL

    def _scroll(direction: ScrollDirection, amount: int) -> None:
        flag, sign = _WIN_SCROLL_EVENTS[direction]
        _user32.mouse_event(flag, 0, 0, sign * amount * _WHEEL_DELTA, 0)

    def _release_modifiers(context: str) -> None:
        # Release only keys Windows reports as down: a blind Win or Alt key-up
        # could open Start or the menu bar.
        held = [
            name
            for vk, name in _WIN_MODIFIER_KEYS.items()
            if _user32.GetAsyncKeyState(vk) & _KEY_DOWN_BIT
        ]
        if not held:
            return
        logger.warning("Modifier keys still down %s: %s", context, ", ".join(held))
        for name in held:
            pyautogui.keyUp(name, _pause=False)

    def _dpi_awareness_note() -> str:
        aware = bool(_user32.IsProcessDPIAware())
        return f", DPI-aware: {'yes' if aware else 'no'}"

else:

    def _scroll(direction: ScrollDirection, amount: int) -> None:
        raise ExecutionError("Scrolling is not supported on this platform")

    def _release_modifiers(context: str) -> None:
        return None

    def _dpi_awareness_note() -> str:
        return ""


def _screeninfo_size() -> tuple[int, int] | None:
    if not _SCREENINFO_AVAILABLE:
        return None
    try:
        monitors = get_monitors()
        if monitors:
            return monitors[0].width, monitors[0].height
    except (IndexError, RuntimeError):
        pass
    return None


class PyAutoGuiDesktopBackend:
    def __init__(self) -> None:
        self._size_lock = threading.Lock()
        # Size of the latest screenshot: the space the server's coordinates
        # come from. None until the first capture.
        self._capture_size: tuple[int, int] | None = None

    @property
    def name(self) -> str:
        return "pyautogui"

    def click(self, x: int, y: int, button: str) -> None:
        pyautogui.click(x=x, y=y, button=button)

    def double_click(self, x: int, y: int) -> None:
        _double_click(x, y)

    def move_to(self, x: int, y: int) -> None:
        pyautogui.moveTo(x=x, y=y)

    def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        duration: float,
    ) -> None:
        pyautogui.moveTo(x=start_x, y=start_y)
        pyautogui.drag(
            xOffset=end_x - start_x,
            yOffset=end_y - start_y,
            duration=duration,
            button=button,
        )

    def scroll(
        self, direction: ScrollDirection, amount: int, x: int | None, y: int | None
    ) -> None:
        if x is not None and y is not None:
            pyautogui.moveTo(x=x, y=y)
        _scroll(direction, amount)

    def type_text(self, text: str, interval: float) -> None:
        _release_modifiers("before typing")
        pyautogui.typewrite(text, interval=interval)

    def resolve_key(self, name: str) -> str | None:
        resolved = _KEY_ALIASES.get(name, name)
        if not pyautogui.isValidKey(resolved):
            return None
        return resolved

    def press_keys(self, keys: Sequence[str]) -> None:
        if len(keys) > 1:
            try:
                pyautogui.hotkey(*keys)
            finally:
                _release_modifiers("after a key combination")
        else:
            pyautogui.press(keys[0])

    def screenshot(self) -> Image.Image:
        image = _capture_screen()
        with self._size_lock:
            first = self._capture_size is None
            self._capture_size = image.size
        if first:
            self._log_screen_spaces(image.size)
        return image

    def capture_size(self) -> tuple[int, int]:
        with self._size_lock:
            size = self._capture_size
        if size is not None:
            return size
        return self.screenshot().size

    def input_size(self) -> tuple[int, int]:
        # Read on every call so a resolution change is seen. macOS: the main
        # display in points; Windows: the primary monitor in this process's
        # DPI space.
        size = pyautogui.size()
        return size.width, size.height

    def _log_screen_spaces(self, capture: tuple[int, int]) -> None:
        # Logged once, on the first capture (the startup screen check), so a
        # Retina or DPI mismatch between capture and input shows in the log.
        try:
            input_w, input_h = self.input_size()
            note = _dpi_awareness_note()
        except Exception as exc:
            logger.warning("Could not read the input screen size: %s", exc)
            return
        logger.info(
            "Screen spaces: capture %dx%d, input %dx%d%s",
            capture[0],
            capture[1],
            input_w,
            input_h,
            note,
        )

    def screen_size(self) -> tuple[int, int]:
        try:
            size = _screeninfo_size()
            if size is not None:
                return size
            fallback = pyautogui.size()
            return fallback.width, fallback.height
        except Exception as exc:
            logger.warning("Could not read the screen size: %s", exc)
            return 0, 0

    def session_info(self) -> dict[str, str]:
        return {}

    def diagnostic_steps(self) -> tuple[DiagnosticStep, ...]:
        return (DiagnosticStep("screen capture", self._check_screen_capture),)

    def _check_screen_capture(self) -> str:
        try:
            self.screenshot()
        except ScreenCaptureError as failure:
            raise RuntimeError(f"{failure}. {_SCREEN_CAPTURE_HINT}") from failure
        return "the screen can be captured"


BACKEND: Final[DesktopBackend] = PyAutoGuiDesktopBackend()
