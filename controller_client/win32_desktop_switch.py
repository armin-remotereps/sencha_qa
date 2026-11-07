"""Switch to a window's virtual desktop on Windows, and back.

The mechanism is the system shortcut Win+Ctrl+Left / Right, pressed once per
desktop between the current and the target one through the runner's own key
path (``desktop_pyautogui.press_combination``, which releases any modifier
still held afterwards). Windows has no documented API to switch desktops;
the undocumented ``IVirtualDesktopManagerInternal`` is not used.

* **Before every press** ``input_guard.ensure_input_not_blocked()``: with an
  elevated window in front UIPI drops the keys silently, so the switch is
  refused instead.
* **Indexes** come from the registry desktop order (``VirtualDesktopIDs``)
  and the documented ``GetWindowDesktopId``; an unreadable order refuses the
  switch ("desktop order unavailable") before anything is pressed.
* **The current desktop** is the one most uncloaked current windows report
  (``IsWindowOnCurrentVirtualDesktop`` over the inventory, whose desktop ids
  are read once), else the registry's ``CurrentVirtualDesktop`` (an empty
  desktop has no windows to tell).
* **Every step is verified:** an intermediate step by the current desktop
  index, the last one by the target being on the current desktop and not
  cloaked; on the way back by the anchor (a window of the original desktop)
  the same way, or, for an empty original desktop, by the current index.
* **Restore** never raises; it presses back from wherever the desktop is now
  (falling back to the number of steps pressed when the current desktop
  cannot be told) and reports whether the original desktop is shown again.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Final

from controller_client.desktop_switch import (
    DESKTOP_SWITCH_BUDGET_SECONDS,
    DesktopSwitcher,
    DesktopSwitchError,
    UnsupportedDesktopSwitcher,
    poll_until,
)
from controller_client.window_model import DesktopInfo, DesktopWindow

logger = logging.getLogger(__name__)

ELEVATED_WINDOW_DETAIL: Final[str] = (
    "an administrator window is in front; run the Test Runner as Administrator"
)
ORDER_UNAVAILABLE_DETAIL: Final[str] = "desktop order unavailable"
MANAGER_UNAVAILABLE_DETAIL: Final[str] = "virtual desktops are not available"
WINDOW_DESKTOP_UNKNOWN_DETAIL: Final[str] = "the window's desktop could not be read"
NOT_VERIFIED_DETAIL: Final[str] = "the desktop switch could not be verified"
KEYS_FAILED_DETAIL: Final[str] = "the desktop switch shortcut could not be pressed"
# Wait after a step when neither desktop can be told apart (no windows on
# them and no registry value): about one switch animation.
_BLIND_STEP_SECONDS: Final[float] = 0.5


def _hwnd(window: DesktopWindow) -> int | None:
    try:
        return int(window.window_id)
    except ValueError:
        return None


if sys.platform == "win32":
    from controller_client import win32_virtual_desktops
    from controller_client.desktop_pyautogui import press_combination
    from controller_client.exceptions import InputBlockedError
    from controller_client.input_guard import ensure_input_not_blocked
    from controller_client.win32_virtual_desktops import VirtualDesktopManager
    from controller_client.win32_windows import cloak_flags

    _STEP_KEYS: Final[dict[int, tuple[str, ...]]] = {
        1: ("winleft", "ctrl", "right"),
        -1: ("winleft", "ctrl", "left"),
    }

    def _drawable(manager: VirtualDesktopManager, hwnd: int) -> bool:
        return manager.is_on_current(hwnd) is True and cloak_flags(hwnd) == 0

    class _Win32DesktopSwitcher:
        def __init__(self, windows: Sequence[DesktopWindow]) -> None:
            self._hwnds = tuple(
                hwnd
                for hwnd in (_hwnd(w) for w in windows if not w.app_cloaked)
                if hwnd is not None
            )
            self._changed = False
            self._order: tuple[uuid.UUID, ...] = ()
            # 0-based index of each known window's desktop.
            self._desktop_of: dict[int, int] = {}
            self._recorded: int | None = None
            self._anchor: int | None = None
            # Net steps pressed (+1 right, -1 left) since the recording.
            self._pressed = 0

        @property
        def changed(self) -> bool:
            return self._changed

        def switch_to(self, window: DesktopWindow) -> None:
            deadline = time.monotonic() + DESKTOP_SWITCH_BUDGET_SECONDS
            self._guard_input()
            hwnd = _hwnd(window)
            if hwnd is None:
                raise DesktopSwitchError(WINDOW_DESKTOP_UNKNOWN_DETAIL)
            order = win32_virtual_desktops.desktop_order()
            if not order:
                raise DesktopSwitchError(ORDER_UNAVAILABLE_DETAIL)
            self._order = order
            with win32_virtual_desktops.open_manager() as manager:
                if manager is None:
                    raise DesktopSwitchError(MANAGER_UNAVAILABLE_DETAIL)
                target_id = manager.desktop_id(hwnd)
                if target_id is None or target_id not in order:
                    raise DesktopSwitchError(WINDOW_DESKTOP_UNKNOWN_DETAIL)
                target = order.index(target_id)
                self._read_desktops(manager)
                current = self._current_index(manager)
                if current is None:
                    raise DesktopSwitchError(ORDER_UNAVAILABLE_DETAIL)
                self._recorded = current
                self._anchor = self._pick_anchor(manager, current)
                logger.info(
                    "Switching from desktop %d to desktop %d for a capture",
                    current + 1,
                    target + 1,
                )
                arrived = self._move(
                    manager, current, target, deadline, lambda: _drawable(manager, hwnd)
                )
            if not arrived:
                raise DesktopSwitchError(NOT_VERIFIED_DETAIL)

        def restore(self) -> bool:
            if not self._changed or self._recorded is None:
                return True
            try:
                restored = self._restore(self._recorded)
            except Exception:  # never raise: the capture's own status is kept
                logger.warning("Switching back to the desktop failed", exc_info=True)
                return False
            if not restored:
                logger.warning(
                    "Could not switch back to desktop %d after a capture",
                    self._recorded + 1,
                )
            return restored

        def _restore(self, recorded: int) -> bool:
            deadline = time.monotonic() + DESKTOP_SWITCH_BUDGET_SECONDS
            with win32_virtual_desktops.open_manager() as manager:
                if manager is None:
                    return False
                current = self._current_index(manager)
                if current is None:
                    current = recorded + self._pressed
                if not 0 <= current < len(self._order):
                    return False
                anchor = self._anchor

                def home() -> bool:
                    if anchor is not None:
                        return _drawable(manager, anchor)
                    return self._current_index(manager) == recorded

                try:
                    return self._move(manager, current, recorded, deadline, home)
                except DesktopSwitchError as e:
                    logger.warning("Switching back refused: %s", e.detail)
                    return False

        def _guard_input(self) -> None:
            try:
                ensure_input_not_blocked()
            except InputBlockedError as e:
                raise DesktopSwitchError(ELEVATED_WINDOW_DETAIL) from e

        def _press(self, step: int) -> None:
            self._guard_input()
            self._changed = True
            try:
                press_combination(_STEP_KEYS[step])
            except Exception as e:
                raise DesktopSwitchError(KEYS_FAILED_DETAIL) from e
            self._pressed += step

        def _move(
            self,
            manager: VirtualDesktopManager,
            start: int,
            goal: int,
            deadline: float,
            arrived: Callable[[], bool],
        ) -> bool:
            """Press from desktop ``start`` to ``goal``, verifying each step;
            the last one (or no step at all) by ``arrived``."""
            position = start
            step = 1 if goal > start else -1
            while position != goal:
                before = self._current_index(manager)
                self._press(step)
                position += step
                if position != goal and not self._wait_for(
                    manager, position, before, deadline
                ):
                    return False
            remaining = max(deadline - time.monotonic(), 0.0)
            return poll_until(arrived, budget_seconds=remaining)

        def _wait_for(
            self,
            manager: VirtualDesktopManager,
            expected: int,
            before: int | None,
            deadline: float,
        ) -> bool:
            if before is None:
                # Neither desktop can be told: let the animation finish.
                time.sleep(
                    min(_BLIND_STEP_SECONDS, max(deadline - time.monotonic(), 0))
                )
                return True

            def reached() -> bool:
                index = self._current_index(manager)
                # None: left a desktop that could be told for one that cannot.
                return index is None or index == expected

            remaining = max(deadline - time.monotonic(), 0.0)
            return poll_until(reached, budget_seconds=remaining)

        def _read_desktops(self, manager: VirtualDesktopManager) -> None:
            for hwnd in self._hwnds:
                desktop = manager.desktop_id(hwnd)
                if desktop is not None and desktop in self._order:
                    self._desktop_of[hwnd] = self._order.index(desktop)

        def _current_index(self, manager: VirtualDesktopManager) -> int | None:
            votes: dict[int, int] = {}
            for hwnd, index in self._desktop_of.items():
                if _drawable(manager, hwnd):
                    votes[index] = votes.get(index, 0) + 1
            if votes:
                return max(votes, key=lambda index: votes[index])
            current = win32_virtual_desktops.registry_current_desktop()
            if current is not None and current in self._order:
                return self._order.index(current)
            return None

        def _pick_anchor(
            self, manager: VirtualDesktopManager, current: int
        ) -> int | None:
            # Topmost window of the current desktop, as the inventory listed it.
            for hwnd in self._hwnds:
                if self._desktop_of.get(hwnd) == current and _drawable(manager, hwnd):
                    return hwnd
            return None

    def create_switcher(
        info: DesktopInfo, windows: Sequence[DesktopWindow]
    ) -> DesktopSwitcher:
        return _Win32DesktopSwitcher(windows)

else:

    def create_switcher(
        info: DesktopInfo, windows: Sequence[DesktopWindow]
    ) -> DesktopSwitcher:
        return UnsupportedDesktopSwitcher("desktop switching needs Windows")
