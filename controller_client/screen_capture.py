from __future__ import annotations

import logging
import platform
import subprocess
import tempfile
from pathlib import Path

import pyautogui
from PIL import Image

from controller_client.exceptions import ScreenCaptureError

logger = logging.getLogger(__name__)

_MACOS = "Darwin"
_SCREENCAPTURE_TIMEOUT_S: float = 30.0

MACOS_PERMISSION_HINT = (
    "macOS refused to hand over the screen. Grant Screen Recording to the "
    "application that launches this controller (Terminal, iTerm, or the "
    "Python binary) under System Settings > Privacy & Security > Screen "
    "Recording, then fully quit and relaunch it -- macOS only applies the "
    "permission to a newly started process."
)


def capture_screen() -> Image.Image:
    """Capture the whole screen, or raise ScreenCaptureError explaining why not.

    On macOS this drives ``screencapture`` directly rather than going through
    ``pyautogui``. Pillow's ``ImageGrab.grab()`` shells out to the same tool
    but discards its exit status, so a permission refusal left an empty temp
    file and surfaced as a decode failure ("cannot identify image file") that
    named a temp path instead of the missing permission.
    """
    if platform.system() == _MACOS:
        return _capture_screen_macos()
    return _capture_screen_pyautogui()


def _capture_screen_macos() -> Image.Image:
    with tempfile.TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "screen.png"
        completed = _run_screencapture(target)

        if completed.returncode != 0 or not _has_image_bytes(target):
            raise ScreenCaptureError(_macos_failure_message(completed))

        try:
            with Image.open(target) as image:
                image.load()
                # Detach from the temp file before the directory is removed.
                captured: Image.Image = image.copy()
                return captured
        except OSError as exc:
            raise ScreenCaptureError(
                f"screencapture wrote a file that could not be decoded ({exc}). "
                f"{MACOS_PERMISSION_HINT}"
            ) from exc


def _run_screencapture(target: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["screencapture", "-x", str(target)],
            capture_output=True,
            text=True,
            timeout=_SCREENCAPTURE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScreenCaptureError(
            f"screencapture did not finish within {_SCREENCAPTURE_TIMEOUT_S}s"
        ) from exc
    except OSError as exc:
        raise ScreenCaptureError(f"could not run screencapture ({exc})") from exc


def _has_image_bytes(target: Path) -> bool:
    return target.exists() and target.stat().st_size > 0


def _macos_failure_message(completed: subprocess.CompletedProcess[str]) -> str:
    stderr = completed.stderr.strip().splitlines()
    reason = stderr[0] if stderr else "screencapture produced no image"
    return f"{reason} (exit code {completed.returncode}). {MACOS_PERMISSION_HINT}"


def _capture_screen_pyautogui() -> Image.Image:
    try:
        return pyautogui.screenshot()
    except Exception as exc:
        raise ScreenCaptureError(f"screen capture failed ({exc})") from exc


def describe_screen_capture_failure() -> str | None:
    """Return why a screen capture would fail right now, or None if it works."""
    try:
        capture_screen()
    except ScreenCaptureError as exc:
        return str(exc)
    return None
