"""The screen and input interface every desktop action goes through.

``desktop.py`` picks the implementation for this platform: pyautogui on
Windows and macOS, the GNOME Wayland backend on Linux.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from PIL import Image

from controller_client.diagnostics import DiagnosticStep
from controller_client.protocol import ScrollDirection


class DesktopBackend(Protocol):
    """Pointer methods (click, double_click, move_to, drag, scroll) take
    input-space coordinates: the executor maps screenshot pixels with
    ``pointer_mapping.map_to_input`` before calling them."""

    @property
    def name(self) -> str:
        """Short backend name for logs and diagnose output."""
        ...

    def click(self, x: int, y: int, button: str) -> None: ...

    def double_click(self, x: int, y: int) -> None: ...

    def move_to(self, x: int, y: int) -> None: ...

    def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str,
        duration: float,
    ) -> None: ...

    def scroll(
        self, direction: ScrollDirection, amount: int, x: int | None, y: int | None
    ) -> None:
        """Turn the wheel ``amount`` notches; x/y None scroll at the pointer."""
        ...

    def type_text(self, text: str, interval: float) -> None: ...

    def resolve_key(self, name: str) -> str | None:
        """Apply the backend's aliases; None when the key is unknown."""
        ...

    def press_keys(self, keys: Sequence[str]) -> None:
        """Press names resolve_key returned: one key presses it, several are a hotkey."""
        ...

    def screenshot(self) -> Image.Image:
        """Capture the screen or raise ScreenCaptureError with the reason and remedy."""
        ...

    def screen_size(self) -> tuple[int, int]:
        """Screen size in screenshot pixels; (0, 0) when unknown, never raises."""
        ...

    def capture_size(self) -> tuple[int, int]:
        """Size of the latest screenshot; captures one when there is none yet."""
        ...

    def input_size(self) -> tuple[int, int]:
        """Size of the coordinate space pointer input takes, read at call time."""
        ...

    def session_info(self) -> dict[str, str]:
        """Extra system_info keys describing the desktop session."""
        ...

    def diagnostic_steps(self) -> tuple[DiagnosticStep, ...]: ...
