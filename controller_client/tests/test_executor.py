from __future__ import annotations

from collections.abc import Callable

import pytest

from controller_client import executor
from controller_client.exceptions import InputBlockedError
from controller_client.protocol import (
    ClickPayload,
    HoverPayload,
    KeyPressPayload,
    TypeTextPayload,
)

EXECUTOR = "controller_client.executor"


def _blocked() -> None:
    raise InputBlockedError("Windows will discard synthesized input")


def _passes() -> None:
    return None


class _CallRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


_INPUT_ACTIONS: list[tuple[Callable[[], object], str]] = [
    (lambda: executor.execute_click(ClickPayload(x=10, y=20, button="left")), "click"),
    (lambda: executor.execute_hover(HoverPayload(x=10, y=20)), "moveTo"),
    (lambda: executor.execute_type_text(TypeTextPayload(text="hi", interval=0.0)), "typewrite"),
    (lambda: executor.execute_key_press(KeyPressPayload(keys="enter")), "press"),
]


@pytest.mark.parametrize(("action", "pyautogui_fn"), _INPUT_ACTIONS)
def test_blocked_input_raises_before_touching_pyautogui(
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[], object],
    pyautogui_fn: str,
) -> None:
    recorder = _CallRecorder()
    monkeypatch.setattr(f"{EXECUTOR}.ensure_input_not_blocked", _blocked)
    monkeypatch.setattr(f"{EXECUTOR}.pyautogui.{pyautogui_fn}", recorder)

    with pytest.raises(InputBlockedError):
        action()

    assert recorder.calls == []


def test_click_dispatches_to_pyautogui_when_input_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _CallRecorder()
    monkeypatch.setattr(f"{EXECUTOR}.ensure_input_not_blocked", _passes)
    monkeypatch.setattr(f"{EXECUTOR}.pyautogui.click", recorder)

    result = executor.execute_click(ClickPayload(x=10, y=20, button="right"))

    assert result.success is True
    assert result.message == "Clicked (10, 20) with right button"
    assert recorder.calls == [((), {"x": 10, "y": 20, "button": "right"})]
