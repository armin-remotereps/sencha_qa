"""Switch to a window's virtual desktop for one capture, and back.

This module holds the ``DesktopSwitcher`` protocol every platform implements, the
budgets and the shared poll helper; the platform switchers live in
``win32_desktop_switch``, ``macos_desktop_switch``, ``x11_desktop_switch``,
``wayland_wlroots`` and ``gnome_shell_capture`` (see ``capture_platform.py``).

One switcher per capture, created from an inventory taken just before the
switch, after the person window is hidden, holding only app windows that are
not the runner's own (``window_capture_policy.switcher_windows``;
``SwitcherFactory``). The service drives it from one worker thread::

    switcher = create_switcher(info, windows)
    try:
        switcher.switch_to(target)        # record, switch, verify
        ...                               # settle, re-enumerate, grab, freshness
    finally:
        restored = switcher.restore() if switcher.changed else None

``switch_to`` performs the whole "record, switch, verify" sequence:

1. **Record** the current desktop (an identity it can verify later), the
   foreground window or app and one anchor window visible on the current
   desktop (platform-specific handling of an empty desktop). A platform that
   could not come back (macOS with no window on the current Space) refuses
   here, before changing anything.
2. **Switch** with the platform mechanism.
3. **Verify** with ``poll_until`` every DESKTOP_SWITCH_POLL_SECONDS until the
   target window is on the current desktop and drawable, within
   DESKTOP_SWITCH_BUDGET_SECONDS.

It raises ``DesktopSwitchError`` with a user-safe detail on any failure
(or ``CaptureUnavailableError`` while recording, before any change, when
the platform's helper or session cannot be reached);
``changed`` tells whether the desktop may have been changed (the mechanism
ran), so the service still calls ``restore``. Settling
(DESKTOP_SWITCH_SETTLE_MS), ``delay_ms`` and the freshness check are the
service's (``capture_freshness.py``), not the switcher's.

``restore`` switches back to the recorded desktop with the same mechanism,
verified against the recorded identity or anchor within the budget, and
returns whether that worked. It never raises (failures are logged as a
warning, without titles) and does no explicit focus change beyond what the
platform's switch back does (macOS: activate the recorded app and raise its
recorded window). It is safe to call once after any ``switch_to`` outcome.

All methods block and run on the capture worker thread (``asyncio.to_thread``),
never on the event loop; each initialises its own per-thread resources (COM,
X display, D-Bus loop thread).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Final, Protocol

from controller_client.window_model import DesktopInfo, DesktopWindow

# Longest wait for one switch (or switch back) to be verified.
DESKTOP_SWITCH_BUDGET_SECONDS: Final[float] = 6.0
# Interval between two verification checks.
DESKTOP_SWITCH_POLL_SECONDS: Final[float] = 0.1
# Least wait after a verified switch before the first grab (the service's).
DESKTOP_SWITCH_SETTLE_MS: Final[int] = 500


class DesktopSwitchError(Exception):
    """The switch could not be done or verified.

    ``detail`` is plain user-safe text (no window titles), for example "an
    administrator window is in front; run the Test Runner as Administrator";
    the service replies ``desktop_switch_failed`` with it.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class DesktopSwitcher(Protocol):
    """One capture's switch to a window's desktop and back (module docstring)."""

    @property
    def changed(self) -> bool:
        """True once the switch mechanism ran (the shown desktop may differ
        from the recorded one), even when ``switch_to`` then raised."""
        ...

    def switch_to(self, window: DesktopWindow) -> None:
        """Record the current desktop, switch to ``window``'s desktop and
        verify it within DESKTOP_SWITCH_BUDGET_SECONDS. Called at most once
        per switcher.

        Raises ``DesktopSwitchError`` when the switch cannot be done or
        verified (``changed`` tells whether the mechanism ran). May also
        raise ``window_model.CaptureUnavailableError`` (``helper_missing``,
        ``not_interactive``, ``capture_failed``) when the platform's helper
        or session cannot be reached; that happens only while recording,
        before anything changes, so ``changed`` stays False. The service
        replies with that error's own status."""
        ...

    def restore(self) -> bool:
        """Switch back to the recorded desktop and verify it; True when the
        recorded desktop is shown again (also when nothing had changed).
        Never raises."""
        ...


# Builds one switcher from the inventory taken just before the switch: the
# session's desktops and its app windows, without the runner's own (the
# anchor is chosen from them).
SwitcherFactory = Callable[[DesktopInfo, Sequence[DesktopWindow]], DesktopSwitcher]


class UnsupportedDesktopSwitcher:
    """A switcher for a platform path that cannot switch desktops: every
    ``switch_to`` raises ``DesktopSwitchError(detail)`` without changing
    anything."""

    def __init__(self, detail: str) -> None:
        self._detail = detail

    @property
    def changed(self) -> bool:
        return False

    def switch_to(self, window: DesktopWindow) -> None:
        raise DesktopSwitchError(self._detail)

    def restore(self) -> bool:
        return True


def poll_until(
    predicate: Callable[[], bool],
    *,
    budget_seconds: float = DESKTOP_SWITCH_BUDGET_SECONDS,
    interval_seconds: float = DESKTOP_SWITCH_POLL_SECONDS,
) -> bool:
    """Call ``predicate`` until it returns True or the budget is spent.

    Checks once at once, then every ``interval_seconds`` (monotonic clock),
    and always once more at the deadline. Returns whether it became True.
    Exceptions from ``predicate`` propagate; a platform that expects
    transient errors catches them inside its predicate. Blocking: call it
    from the worker thread only.
    """
    deadline = time.monotonic() + budget_seconds
    while True:
        if predicate():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval_seconds, remaining))
