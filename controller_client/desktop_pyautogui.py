"""The Windows and macOS desktop backend, built on pyautogui.

Imported only when ``sys.platform != "linux"`` (see ``desktop.py``); Linux
uses the GNOME Wayland backend instead.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import Final

import pyautogui
from PIL import Image

from controller_client.desktop_backend import DesktopBackend
from controller_client.diagnostics import DiagnosticStep
from controller_client.exceptions import ExecutionError, ScreenCaptureError

if sys.platform == "darwin":
    import Quartz

    from controller_client.screen_capture_macos import capture_screen_macos

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

    def type_text(self, text: str, interval: float) -> None:
        pyautogui.typewrite(text, interval=interval)

    def resolve_key(self, name: str) -> str | None:
        resolved = _KEY_ALIASES.get(name, name)
        if not pyautogui.isValidKey(resolved):
            return None
        return resolved

    def press_keys(self, keys: Sequence[str]) -> None:
        if len(keys) > 1:
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(keys[0])

    def screenshot(self) -> Image.Image:
        return _capture_screen()

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
