from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from controller_client import screen_capture
from controller_client.exceptions import ScreenCaptureError

MODULE = "controller_client.screen_capture"


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["screencapture"], returncode=returncode, stdout="", stderr=stderr
    )


def _force_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{MODULE}.platform.system", lambda: "Darwin")


def _fake_screencapture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int,
    stderr: str = "",
    write_image: bool = False,
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if write_image:
            Image.new("RGB", (4, 2), color="red").save(args[-1])
        return _completed(returncode, stderr)

    monkeypatch.setattr(f"{MODULE}.subprocess.run", fake_run)


def test_macos_permission_refusal_names_the_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_macos(monkeypatch)
    _fake_screencapture(
        monkeypatch, returncode=1, stderr="could not create image from display\n"
    )

    with pytest.raises(ScreenCaptureError) as excinfo:
        screen_capture.capture_screen()

    message = str(excinfo.value)
    assert "could not create image from display" in message
    assert "Screen Recording" in message


def test_macos_zero_byte_output_is_a_failure_not_a_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_macos(monkeypatch)
    _fake_screencapture(monkeypatch, returncode=0)

    with pytest.raises(ScreenCaptureError) as excinfo:
        screen_capture.capture_screen()

    assert "cannot identify image file" not in str(excinfo.value)
    assert "Screen Recording" in str(excinfo.value)


def test_macos_success_returns_the_captured_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_macos(monkeypatch)
    _fake_screencapture(monkeypatch, returncode=0, write_image=True)

    image = screen_capture.capture_screen()

    assert image.size == (4, 2)


def test_macos_capture_survives_the_temp_directory_being_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_macos(monkeypatch)
    captured_paths: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_paths.append(args[-1])
        Image.new("RGB", (3, 3), color="blue").save(args[-1])
        return _completed(0)

    monkeypatch.setattr(f"{MODULE}.subprocess.run", fake_run)

    image = screen_capture.capture_screen()

    assert not Path(captured_paths[0]).exists()
    assert image.getpixel((0, 0)) == (0, 0, 255)


def test_non_macos_falls_back_to_pyautogui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{MODULE}.platform.system", lambda: "Linux")
    expected = Image.new("RGB", (5, 5))
    monkeypatch.setattr(f"{MODULE}.pyautogui.screenshot", lambda: expected)

    assert screen_capture.capture_screen() is expected


def test_non_macos_capture_failure_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{MODULE}.platform.system", lambda: "Linux")

    def boom() -> Image.Image:
        raise RuntimeError("no display")

    monkeypatch.setattr(f"{MODULE}.pyautogui.screenshot", boom)

    with pytest.raises(ScreenCaptureError, match="no display"):
        screen_capture.capture_screen()


def test_describe_failure_reports_none_when_capture_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{MODULE}.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        f"{MODULE}.pyautogui.screenshot", lambda: Image.new("RGB", (1, 1))
    )

    assert screen_capture.describe_screen_capture_failure() is None


def test_describe_failure_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_macos(monkeypatch)
    _fake_screencapture(
        monkeypatch, returncode=1, stderr="could not create image from display"
    )

    failure = screen_capture.describe_screen_capture_failure()

    assert failure is not None
    assert "Screen Recording" in failure
