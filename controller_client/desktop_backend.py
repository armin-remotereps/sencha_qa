"""The screen and input interface every desktop action goes through.

``desktop.py`` picks the implementation for this platform: pyautogui on
Windows and macOS, the GNOME Wayland backend on Linux.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from PIL import Image

from controller_client.diagnostics import DiagnosticStep


class DesktopBackend(Protocol):
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

    def session_info(self) -> dict[str, str]:
        """Extra system_info keys describing the desktop session."""
        ...

    def diagnostic_steps(self) -> tuple[DiagnosticStep, ...]: ...
