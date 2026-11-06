"""W3C touch and key actions sent as plain JSON through the driver.

Built by hand rather than with selenium's ActionBuilder, whose pointer
methods are untyped. Coordinates are input-space (Android pixels, iOS
points), already mapped by the executor.
"""

from __future__ import annotations

from typing import Final

from appium.webdriver.webdriver import WebDriver
from selenium.webdriver.remote.command import Command

_FINGER_ID: Final[str] = "finger1"
_KEYBOARD_ID: Final[str] = "keyboard"
_TAP_HOLD_MS: Final[int] = 80
_DOUBLE_TAP_GAP_MS: Final[int] = 100
# A drag holds briefly first so lists and sliders take it as a drag, not a fling.
_DRAG_HOLD_MS: Final[int] = 300

type ActionItem = dict[str, object]


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def _move(x: int, y: int, duration_ms: int = 0) -> ActionItem:
    return {
        "type": "pointerMove",
        "duration": duration_ms,
        "x": x,
        "y": y,
        "origin": "viewport",
    }


def _down() -> ActionItem:
    return {"type": "pointerDown", "button": 0}


def _up() -> ActionItem:
    return {"type": "pointerUp", "button": 0}


def _pause(duration_ms: int) -> ActionItem:
    return {"type": "pause", "duration": duration_ms}


def _perform_finger(driver: WebDriver, actions: list[ActionItem]) -> None:
    driver.execute(
        Command.W3C_ACTIONS,
        {
            "actions": [
                {
                    "type": "pointer",
                    "id": _FINGER_ID,
                    "parameters": {"pointerType": "touch"},
                    "actions": actions,
                }
            ]
        },
    )


def tap(driver: WebDriver, x: int, y: int) -> None:
    _perform_finger(driver, [_move(x, y), _down(), _pause(_TAP_HOLD_MS), _up()])


def double_tap(driver: WebDriver, x: int, y: int) -> None:
    single = [_down(), _pause(_TAP_HOLD_MS), _up()]
    _perform_finger(driver, [_move(x, y), *single, _pause(_DOUBLE_TAP_GAP_MS), *single])


def long_press(driver: WebDriver, x: int, y: int, duration: float) -> None:
    _perform_finger(driver, [_move(x, y), _down(), _pause(_ms(duration)), _up()])


def swipe(
    driver: WebDriver,
    start: tuple[int, int],
    end: tuple[int, int],
    duration: float,
) -> None:
    """One finger from ``start`` to ``end`` in ``duration`` seconds."""
    _perform_finger(
        driver,
        [
            _move(*start),
            _down(),
            _move(*end, duration_ms=max(1, _ms(duration))),
            _up(),
        ],
    )


def drag(
    driver: WebDriver,
    start: tuple[int, int],
    end: tuple[int, int],
    duration: float,
) -> None:
    """Hold at ``start``, move to ``end`` and release."""
    _perform_finger(
        driver,
        [
            _move(*start),
            _down(),
            _pause(_DRAG_HOLD_MS),
            _move(*end, duration_ms=max(1, _ms(duration))),
            _up(),
        ],
    )


def send_keys(driver: WebDriver, values: list[str], interval: float) -> None:
    """Press each value (a character or a WebDriver key code) in turn."""
    actions: list[ActionItem] = []
    gap = _ms(interval)
    for value in values:
        actions.append({"type": "keyDown", "value": value})
        actions.append({"type": "keyUp", "value": value})
        if gap:
            actions.append(_pause(gap))
    if not actions:
        return
    driver.execute(
        Command.W3C_ACTIONS,
        {"actions": [{"type": "key", "id": _KEYBOARD_ID, "actions": actions}]},
    )
