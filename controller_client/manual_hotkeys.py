"""Manual run Pass / Fail shortcuts: platform selection and the probe.

``ManualHotkeys`` lives in the window process. It is created on the Tk thread
(the macOS backend registers there), picks the platform backend and owns the
queue the backends put ``HotkeyKind`` events on; the window drains it from its
``poll`` loop. See ``manual_hotkeys_base`` for the backend contract.

Neither this module nor a backend imports tkinter: the window process imports
it at start and reports a missing tkinter itself (``--probe``, exit 3).
"""

import queue
import sys
import time
from typing import Protocol

from controller_client.manual_hotkeys_base import (
    OFF_STATUS,
    HotkeyBackend,
    HotkeyKind,
    HotkeyQueue,
    HotkeyState,
    HotkeyStatus,
    StaticBackend,
    focus_only,
)
from controller_client.manual_hotkeys_linux import select_backend
from controller_client.manual_hotkeys_macos import MacHotkeys
from controller_client.manual_hotkeys_windows import WindowsHotkeys

# The interactive probe waits this long for the tester's key press.
PROBE_PRESS_TIMEOUT_S = 20.0
# Any probe waits this long for a pending registration (a desktop dialog).
PROBE_PENDING_TIMEOUT_S = 60.0
_PROBE_TICK_S = 0.05


class EventPump(Protocol):
    """What the probe needs from the Tk root: run pending events."""

    def update(self) -> None: ...


def _create_backend(
    events: HotkeyQueue, *, x11_window_id: int, dry_run: bool
) -> HotkeyBackend:
    if sys.platform == "win32":
        return WindowsHotkeys(events, dry_run=dry_run)
    if sys.platform == "darwin":
        return MacHotkeys(events, dry_run=dry_run)
    if sys.platform.startswith("linux"):
        return select_backend(events, x11_window_id=x11_window_id, dry_run=dry_run)
    return StaticBackend(focus_only("No global shortcut service on this system"))


class ManualHotkeys:
    """The window's shortcuts: enable while a claimed case is not paused.

    ``x11_window_id`` is the Tk root's ``wm_frame()`` id (Linux portal parent
    window); 0 when unknown. ``dry_run`` checks availability without showing
    a desktop permission dialog (``--probe-shortcuts`` without
    ``--interactive``).
    """

    def __init__(
        self, *, enabled: bool, x11_window_id: int = 0, dry_run: bool = False
    ) -> None:
        self._events: HotkeyQueue = queue.Queue()
        self._backend: HotkeyBackend = (
            _create_backend(self._events, x11_window_id=x11_window_id, dry_run=dry_run)
            if enabled
            else StaticBackend(OFF_STATUS)
        )

    @property
    def status(self) -> HotkeyStatus:
        return self._backend.status

    def set_wanted(self, wanted: bool) -> None:
        if wanted:
            self._backend.enable()
        else:
            self._backend.disable()

    def drain(self) -> list[HotkeyKind]:
        """Events since the last drain, oldest first (Tk thread)."""
        kinds: list[HotkeyKind] = []
        while True:
            try:
                kinds.append(self._events.get_nowait())
            except queue.Empty:
                return kinds

    def close(self) -> None:
        self._backend.close()


def probe_shortcuts(
    root: EventPump, *, interactive: bool, x11_window_id: int = 0
) -> HotkeyStatus:
    """Register, report and release (``--probe-shortcuts``).

    Interactive: also wait up to PROBE_PRESS_TIMEOUT_S for the Pass
    combination; a registration that never delivers it is reported as not
    working. Pumps Tk while waiting (the macOS handler runs on the Tk loop).
    """
    hotkeys = ManualHotkeys(
        enabled=True, x11_window_id=x11_window_id, dry_run=not interactive
    )
    try:
        hotkeys.set_wanted(True)
        deadline = time.monotonic() + PROBE_PENDING_TIMEOUT_S
        while (
            hotkeys.status.state is HotkeyState.PENDING and time.monotonic() < deadline
        ):
            root.update()
            time.sleep(_PROBE_TICK_S)
        status = hotkeys.status
        if not interactive or status.state is not HotkeyState.ACTIVE:
            return status
        print(
            f"Press {status.triggers[HotkeyKind.PASS]} within "
            f"{PROBE_PRESS_TIMEOUT_S:.0f} seconds…",
            flush=True,
        )
        deadline = time.monotonic() + PROBE_PRESS_TIMEOUT_S
        while time.monotonic() < deadline:
            root.update()
            if HotkeyKind.PASS in hotkeys.drain():
                return status
            time.sleep(_PROBE_TICK_S)
        return HotkeyStatus(
            state=HotkeyState.FOCUS_ONLY,
            reason="The shortcut was registered but no key press arrived",
            triggers=status.triggers,
        )
    finally:
        hotkeys.close()
