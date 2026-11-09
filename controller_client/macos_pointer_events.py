"""Quartz mouse and wheel events the macOS pyautogui backend posts itself.

Imported only when ``sys.platform == "darwin"`` (see ``desktop_pyautogui``).
``flags`` is a ``kCGEventFlagMask*`` mask set on every posted event, so an app
sees the held modifier keys on the down, up, dragged, moved and wheel events
alike. Flags 0 sets nothing: the event keeps the flags Quartz gives it, as
before modifiers existed.
"""

from __future__ import annotations

import logging
import time
from typing import Final, NamedTuple

import pyautogui
import Quartz

from controller_client.exceptions import ExecutionError
from controller_client.protocol import ScrollDirection

logger = logging.getLogger(__name__)

# Lines per notch, and (dy, dx) per direction for
# CGEventCreateScrollWheelEvent: wheel 1 is vertical, wheel 2 horizontal.
_LINES_PER_NOTCH: Final = 3
_SCROLL_DELTAS: Final[dict[ScrollDirection, tuple[int, int]]] = {
    ScrollDirection.UP: (_LINES_PER_NOTCH, 0),
    ScrollDirection.DOWN: (-_LINES_PER_NOTCH, 0),
    ScrollDirection.LEFT: (0, _LINES_PER_NOTCH),
    ScrollDirection.RIGHT: (0, -_LINES_PER_NOTCH),
}

_DRAG_STEPS_PER_SECOND: Final = 60
_DRAG_MIN_STEPS: Final = 5


class _ButtonEvents(NamedTuple):
    """The Quartz event types and button number of one mouse button."""

    down: int
    up: int
    dragged: int
    button: int


_BUTTONS: Final[dict[str, _ButtonEvents]] = {
    "left": _ButtonEvents(
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
        Quartz.kCGEventLeftMouseDragged,
        Quartz.kCGMouseButtonLeft,
    ),
    "right": _ButtonEvents(
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGEventRightMouseDragged,
        Quartz.kCGMouseButtonRight,
    ),
    "middle": _ButtonEvents(
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
        Quartz.kCGEventOtherMouseDragged,
        Quartz.kCGMouseButtonCenter,
    ),
}


def _button_events(button: str) -> _ButtonEvents:
    events = _BUTTONS.get(button)
    if events is None:
        raise ExecutionError(
            f"Unknown mouse button {button!r}; use 'left', 'right' or 'middle'."
        )
    return events


def _post_mouse(
    event_type: int,
    x: float,
    y: float,
    button: int,
    flags: int,
    click_state: int | None = None,
) -> None:
    event = Quartz.CGEventCreateMouseEvent(
        None, event_type, Quartz.CGPointMake(x, y), button
    )
    if event is None:
        raise ExecutionError("Could not create a mouse event")
    if click_state is not None:
        Quartz.CGEventSetIntegerValueField(
            event, Quartz.kCGMouseEventClickState, click_state
        )
    if flags:
        Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def move(x: int, y: int, flags: int) -> None:
    _post_mouse(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft, flags)


def click(x: int, y: int, button: str, flags: int) -> None:
    events = _button_events(button)
    move(x, y, flags)
    _post_mouse(events.down, x, y, events.button, flags, click_state=1)
    _post_mouse(events.up, x, y, events.button, flags, click_state=1)


def double_click(x: int, y: int, flags: int = 0) -> None:
    # pyautogui 0.9.54 posts two plain down/up pairs on macOS and never sets
    # kCGMouseEventClickState, the field AppKit reads as clickCount, so Cocoa
    # apps saw two single clicks. Post the pairs with click state 1 then 2.
    if flags:
        move(x, y, flags)
    else:
        pyautogui.moveTo(x=x, y=y)
    for click_state in (1, 2):
        for event_type in (
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
        ):
            _post_mouse(
                event_type,
                x,
                y,
                Quartz.kCGMouseButtonLeft,
                flags,
                click_state=click_state,
            )


def drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str,
    duration: float,
    flags: int,
) -> None:
    events = _button_events(button)
    steps = max(_DRAG_MIN_STEPS, round(duration * _DRAG_STEPS_PER_SECOND))
    step_sleep = max(duration, 0.0) / steps
    move(start_x, start_y, flags)
    _post_mouse(events.down, start_x, start_y, events.button, flags, click_state=1)
    x: float = start_x
    y: float = start_y
    try:
        for step in range(1, steps + 1):
            fraction = step / steps
            x = start_x + (end_x - start_x) * fraction
            y = start_y + (end_y - start_y) * fraction
            _post_mouse(events.dragged, x, y, events.button, flags)
            if step_sleep > 0:
                time.sleep(step_sleep)
    except BaseException:
        # The button goes up where the pointer is; a failing up never hides
        # the step's own error.
        try:
            _post_mouse(events.up, x, y, events.button, flags, click_state=1)
        except Exception as exc:
            logger.warning("Mouse button release after a failed drag failed: %s", exc)
        raise
    _post_mouse(events.up, x, y, events.button, flags, click_state=1)


def scroll(direction: ScrollDirection, amount: int, flags: int = 0) -> None:
    dy, dx = _SCROLL_DELTAS[direction]
    for _ in range(amount):
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 2, dy, dx
        )
        if event is None:
            raise ExecutionError("Could not create a scroll wheel event")
        if flags:
            Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
