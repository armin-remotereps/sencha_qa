"""Switch an EWMH window manager's desktop for one capture, and back.

The mechanism is a ``_NET_CURRENT_DESKTOP`` client message to the root window
(SubstructureRedirect | SubstructureNotify), sent for the target desktop and
for the recorded one on the way back:

* **record:** ``_NET_CURRENT_DESKTOP`` read fresh (the 0-based index is the
  identity verified on the way back; X11 needs no anchor window);
* **switch / verify:** until ``_NET_CURRENT_DESKTOP`` equals the window's
  desktop and the window is mapped (viewable), within the budget;
* **restore:** the same message for the recorded index, verified by reading
  ``_NET_CURRENT_DESKTOP``.

A window manager that ignores the message within the budget gives
``DesktopSwitchError``. Each of ``switch_to`` / ``restore`` uses its own
``Display`` (``x11_lib.open_display``), opened and closed in the call.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from controller_client.desktop_switch import (
    DesktopSwitcher,
    DesktopSwitchError,
    poll_until,
)
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopInfo,
    DesktopWindow,
)
from controller_client.x11_lib import X11Display, open_display

logger = logging.getLogger(__name__)

# EWMH source indication: a pager / taskbar (direct user action).
_SOURCE_PAGER: Final = 2
_CURRENT_TIME: Final = 0

_UNKNOWN_DESKTOP_DETAIL: Final = "the window's desktop is not known"
_NO_CURRENT_DETAIL: Final = "the window manager does not report the current desktop"
_REFUSED_DETAIL: Final = "the window manager did not switch the desktop"
_SEND_FAILED_DETAIL: Final = "the desktop switch request could not be sent"


def _current_desktop(display: X11Display) -> int | None:
    return display.cardinal(display.root, "_NET_CURRENT_DESKTOP")


def _request_desktop(display: X11Display, index: int) -> bool:
    return display.send_root_message(
        display.root,
        "_NET_CURRENT_DESKTOP",
        [index, _CURRENT_TIME, _SOURCE_PAGER],
    )


class _X11DesktopSwitcher:
    """``DesktopSwitcher`` over ``_NET_CURRENT_DESKTOP`` (module docstring)."""

    def __init__(self, info: DesktopInfo) -> None:
        self._count = info.count
        self._changed = False
        self._recorded: int | None = None

    @property
    def changed(self) -> bool:
        return self._changed

    def switch_to(self, window: DesktopWindow) -> None:
        if window.desktop.current:
            return
        index = window.desktop.index
        if index is None or index < 1:
            raise DesktopSwitchError(_UNKNOWN_DESKTOP_DETAIL)
        target = index - 1
        if self._count is not None and target >= self._count:
            raise DesktopSwitchError(_UNKNOWN_DESKTOP_DETAIL)
        try:
            xid = int(window.window_id)
        except ValueError:
            raise DesktopSwitchError(_UNKNOWN_DESKTOP_DETAIL) from None
        try:
            with open_display() as display:
                self._switch(display, target, xid)
        except CaptureUnavailableError as exc:
            raise DesktopSwitchError(exc.detail) from exc

    def _switch(self, display: X11Display, target: int, xid: int) -> None:
        recorded = _current_desktop(display)
        if recorded is None:
            raise DesktopSwitchError(_NO_CURRENT_DETAIL)
        self._recorded = recorded
        if recorded == target:
            return
        self._changed = True
        if not _request_desktop(display, target):
            raise DesktopSwitchError(_SEND_FAILED_DETAIL)

        def shown() -> bool:
            if _current_desktop(display) != target:
                return False
            attrs = display.attributes(xid)
            return attrs is not None and attrs.viewable

        if not poll_until(shown):
            logger.warning("X11 desktop switch to %d was not verified", target + 1)
            raise DesktopSwitchError(_REFUSED_DETAIL)
        logger.info("Switched X11 desktop %d -> %d", recorded + 1, target + 1)

    def restore(self) -> bool:
        if not self._changed:
            return True
        recorded = self._recorded
        if recorded is None:
            return False
        try:
            with open_display() as display:
                if _current_desktop(display) == recorded:
                    return True
                if not _request_desktop(display, recorded):
                    logger.warning("X11 desktop switch back could not be sent")
                    return False
                restored = poll_until(lambda: _current_desktop(display) == recorded)
        except Exception:
            logger.warning("X11 desktop switch back failed", exc_info=True)
            return False
        if not restored:
            logger.warning(
                "X11 desktop switch back to %d was not verified", recorded + 1
            )
        return restored


def create_switcher(
    info: DesktopInfo, windows: Sequence[DesktopWindow]
) -> DesktopSwitcher:
    """``desktop_switch.SwitcherFactory`` for X11 (no anchor is needed: the
    desktop index itself is verified)."""
    return _X11DesktopSwitcher(info)
