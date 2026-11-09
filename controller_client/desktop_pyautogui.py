"""The Windows and macOS desktop backend, built on pyautogui.

Imported only when ``sys.platform != "linux"`` (see ``desktop.py``); Linux
uses the GNOME Wayland backend instead.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Final

import pyautogui
from PIL import Image

from controller_client.desktop_backend import DesktopBackend
from controller_client.diagnostics import DiagnosticStep
from controller_client.exceptions import ExecutionError, ScreenCaptureError
from controller_client.protocol import PointerModifier, ScrollDirection

if sys.platform == "darwin":
    import Quartz

    from controller_client import macos_pointer_events
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

# The pyautogui key each pointer modifier holds. primary and meta are both
# Command on macOS, primary and ctrl both Ctrl elsewhere; such a pair is
# pressed once.
if sys.platform == "darwin":
    _POINTER_MODIFIER_KEYS: Final[dict[PointerModifier, str]] = {
        PointerModifier.PRIMARY: "command",
        PointerModifier.CTRL: "ctrl",
        PointerModifier.ALT: "option",
        PointerModifier.SHIFT: "shift",
        PointerModifier.META: "command",
    }
else:
    _POINTER_MODIFIER_KEYS = {
        PointerModifier.PRIMARY: "ctrl",
        PointerModifier.CTRL: "ctrl",
        PointerModifier.ALT: "alt",
        PointerModifier.SHIFT: "shift",
        PointerModifier.META: "winleft",
    }
# Lets the key-downs land before the pointer events.
_POINTER_MODIFIER_SETTLE_SECONDS: Final = 0.02


if sys.platform == "darwin":

    def _double_click(x: int, y: int) -> None:
        macos_pointer_events.double_click(x, y)

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
    _MAC_MODIFIER_MASK: Final = (
        Quartz.kCGEventFlagMaskShift
        | Quartz.kCGEventFlagMaskControl
        | Quartz.kCGEventFlagMaskAlternate
        | Quartz.kCGEventFlagMaskCommand
    )
    # Per modifier flag: its name and the keys that release it. A lone
    # modifier key-up is only a flagsChanged event on macOS, so releasing a
    # side that is not down is safe.
    _MAC_MODIFIER_KEYS: Final = (
        (Quartz.kCGEventFlagMaskShift, "shift", ("shift", "shiftright")),
        (Quartz.kCGEventFlagMaskControl, "ctrl", ("ctrl", "ctrlright")),
        (Quartz.kCGEventFlagMaskAlternate, "option", ("option", "optionright")),
        (Quartz.kCGEventFlagMaskCommand, "command", ("command",)),
    )
    _MODIFIER_SETTLE_SECONDS: Final = 0.02

    def _scroll(direction: ScrollDirection, amount: int) -> None:
        macos_pointer_events.scroll(direction, amount)

    def _modifier_flags(keys: Sequence[str]) -> int:
        # The kCGEventFlagMask* mask of the held pyautogui keys.
        flags = 0
        for mask, name, _ in _MAC_MODIFIER_KEYS:
            if name in keys:
                flags |= mask
        return flags

    def _mask_menu_activation(keys: Sequence[str]) -> None:
        return None

    def _held_modifier_flags() -> int:
        # The session's modifier flags, masked to Shift/Ctrl/Option/Command.
        flags = Quartz.CGEventSourceFlagsState(
            Quartz.kCGEventSourceStateCombinedSessionState
        )
        return flags & _MAC_MODIFIER_MASK

    def held_modifiers() -> tuple[str, ...]:
        flags = _held_modifier_flags()
        return tuple(name for mask, name, _ in _MAC_MODIFIER_KEYS if flags & mask)

    def _release_modifiers(context: str) -> tuple[str, ...]:
        # Typing while Ctrl+Cmd is still down turns a space into Ctrl+Cmd+Space
        # (the emoji picker). Release only what is still set: every key event
        # costs pyautogui's 10 ms catch-up sleep on macOS.
        flags = _held_modifier_flags()
        if not flags:
            return ()
        logger.warning("Modifier flags still set %s: 0x%x", context, flags)
        held: list[str] = []
        for mask, name, keys in _MAC_MODIFIER_KEYS:
            if flags & mask:
                held.append(name)
                for key in keys:
                    pyautogui.keyUp(key, _pause=False)
        time.sleep(_MODIFIER_SETTLE_SECONDS)
        return tuple(held)

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
    # An unassigned virtual key (AutoHotkey's "menu mask"): tapped while Alt or
    # Win is held, so their key-up does not open the menu bar or Start.
    _VK_MENU_MASK: Final = 0xE8
    _KEYEVENTF_KEYUP: Final = 0x0002
    _MENU_ACTIVATING_KEYS: Final = frozenset({"alt", "winleft"})

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
    _user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,  # dwExtraInfo: ULONG_PTR
    ]
    _user32.keybd_event.restype = None

    def _scroll(direction: ScrollDirection, amount: int) -> None:
        flag, sign = _WIN_SCROLL_EVENTS[direction]
        _user32.mouse_event(flag, 0, 0, sign * amount * _WHEEL_DELTA, 0)

    def _modifier_flags(keys: Sequence[str]) -> int:
        # Windows mouse events carry no modifier flags; the keys are down.
        return 0

    def _mask_menu_activation(keys: Sequence[str]) -> None:
        if _MENU_ACTIVATING_KEYS.isdisjoint(keys):
            return
        _user32.keybd_event(_VK_MENU_MASK, 0, 0, 0)
        _user32.keybd_event(_VK_MENU_MASK, 0, _KEYEVENTF_KEYUP, 0)

    def held_modifiers() -> tuple[str, ...]:
        return tuple(
            name
            for vk, name in _WIN_MODIFIER_KEYS.items()
            if _user32.GetAsyncKeyState(vk) & _KEY_DOWN_BIT
        )

    def _release_modifiers(context: str) -> tuple[str, ...]:
        # Release only keys Windows reports as down: a blind Win or Alt key-up
        # could open Start or the menu bar.
        held = held_modifiers()
        if not held:
            return ()
        logger.warning("Modifier keys still down %s: %s", context, ", ".join(held))
        for name in held:
            pyautogui.keyUp(name, _pause=False)
        return held

    def _dpi_awareness_note() -> str:
        aware = bool(_user32.IsProcessDPIAware())
        return f", DPI-aware: {'yes' if aware else 'no'}"

else:

    def _scroll(direction: ScrollDirection, amount: int) -> None:
        raise ExecutionError("Scrolling is not supported on this platform")

    def _modifier_flags(keys: Sequence[str]) -> int:
        return 0

    def _mask_menu_activation(keys: Sequence[str]) -> None:
        return None

    def held_modifiers() -> tuple[str, ...]:
        return ()

    def _release_modifiers(context: str) -> tuple[str, ...]:
        return ()

    def _dpi_awareness_note() -> str:
        return ""


def press_combination(keys: Sequence[str]) -> tuple[str, ...]:
    """Press a key combination, then release any modifier still held.

    Returns the modifiers the release found still down (empty when none).
    ``PyAutoGuiDesktopBackend.press_keys`` uses it at runtime and
    ``diagnose --input`` reads its result, so both run the same code.
    """
    held: tuple[str, ...] = ()
    try:
        pyautogui.hotkey(*keys)
    finally:
        held = _release_modifiers("after a key combination")
    return held


def _pointer_modifier_keys(modifiers: Sequence[PointerModifier]) -> tuple[str, ...]:
    """The pyautogui keys to hold, in the given order, each once."""
    keys: list[str] = []
    for modifier in modifiers:
        key = _POINTER_MODIFIER_KEYS[modifier]
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _with_modifiers(
    modifiers: Sequence[PointerModifier], action: Callable[[int], None]
) -> None:
    """Hold the modifier keys around ``action``; it gets the macOS event flags.

    The keys are released in reverse order, also when the action raises; a
    failed release never hides the action's own error.
    """
    keys = _pointer_modifier_keys(modifiers)
    pressed: list[str] = []
    try:
        for key in keys:
            # Recorded first: a key-down that raised may still have landed.
            pressed.append(key)
            pyautogui.keyDown(key, _pause=False)
        time.sleep(_POINTER_MODIFIER_SETTLE_SECONDS)
        action(_modifier_flags(keys))
    except BaseException:
        _release_pointer_modifiers(pressed, quiet=True)
        raise
    _release_pointer_modifiers(pressed, quiet=False)


def _release_pointer_modifiers(pressed: Sequence[str], quiet: bool) -> None:
    """Release every pressed key, even after one step fails."""
    if not pressed:
        return
    first_error: Exception | None = None
    try:
        _mask_menu_activation(pressed)
    except Exception as exc:
        first_error = exc
    for key in reversed(pressed):
        try:
            pyautogui.keyUp(key, _pause=False)
        except Exception as exc:
            first_error = first_error or exc
    try:
        # Catches a key-up the OS lost, through the same per-platform check
        # as key combinations.
        _release_modifiers("after a modified pointer action")
    except Exception as exc:
        first_error = first_error or exc
    if first_error is None:
        return
    if not quiet:
        # The press itself was done: say so, or the server may repeat it and
        # undo what it did (a second Ctrl+click toggles the selection back).
        raise ExecutionError(
            "The action was done, but releasing the modifier keys failed: "
            f"{first_error}. Do not repeat the action."
        ) from first_error
    logger.warning("Modifier key release failed: %s", first_error)


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


def _pyautogui_drag(
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


class PyAutoGuiDesktopBackend:
    def __init__(self) -> None:
        self._size_lock = threading.Lock()
        # Size of the latest screenshot: the space the server's coordinates
        # come from. None until the first capture.
        self._capture_size: tuple[int, int] | None = None

    @property
    def name(self) -> str:
        return "pyautogui"

    # With modifiers, macOS posts its own Quartz events carrying the flags
    # (macos_pointer_events); Windows runs the same pyautogui calls while the
    # keys are down. Without modifiers nothing below changes.

    def click(
        self,
        x: int,
        y: int,
        button: str,
        *,
        modifiers: Sequence[PointerModifier] = (),
    ) -> None:
        if not modifiers:
            pyautogui.click(x=x, y=y, button=button)
            return

        def action(flags: int) -> None:
            if sys.platform == "darwin":
                macos_pointer_events.click(x, y, button, flags)
            else:
                pyautogui.click(x=x, y=y, button=button)

        _with_modifiers(modifiers, action)

    def double_click(
        self, x: int, y: int, *, modifiers: Sequence[PointerModifier] = ()
    ) -> None:
        if not modifiers:
            _double_click(x, y)
            return

        def action(flags: int) -> None:
            if sys.platform == "darwin":
                macos_pointer_events.double_click(x, y, flags)
            else:
                _double_click(x, y)

        _with_modifiers(modifiers, action)

    def move_to(
        self, x: int, y: int, *, modifiers: Sequence[PointerModifier] = ()
    ) -> None:
        if not modifiers:
            pyautogui.moveTo(x=x, y=y)
            return

        def action(flags: int) -> None:
            if sys.platform == "darwin":
                macos_pointer_events.move(x, y, flags)
            else:
                pyautogui.moveTo(x=x, y=y)

        _with_modifiers(modifiers, action)

    def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        duration: float,
        *,
        modifiers: Sequence[PointerModifier] = (),
    ) -> None:
        if not modifiers:
            _pyautogui_drag(start_x, start_y, end_x, end_y, button, duration)
            return

        def action(flags: int) -> None:
            if sys.platform == "darwin":
                macos_pointer_events.drag(
                    start_x, start_y, end_x, end_y, button, duration, flags
                )
            else:
                _pyautogui_drag(start_x, start_y, end_x, end_y, button, duration)

        _with_modifiers(modifiers, action)

    def scroll(
        self,
        direction: ScrollDirection,
        amount: int,
        x: int | None,
        y: int | None,
        *,
        modifiers: Sequence[PointerModifier] = (),
    ) -> None:
        if not modifiers:
            if x is not None and y is not None:
                pyautogui.moveTo(x=x, y=y)
            _scroll(direction, amount)
            return

        def action(flags: int) -> None:
            if sys.platform == "darwin":
                if x is not None and y is not None:
                    macos_pointer_events.move(x, y, flags)
                macos_pointer_events.scroll(direction, amount, flags)
            else:
                if x is not None and y is not None:
                    pyautogui.moveTo(x=x, y=y)
                _scroll(direction, amount)

        _with_modifiers(modifiers, action)

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
            press_combination(keys)
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
