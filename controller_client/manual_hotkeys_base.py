"""Shared types of the Manual run Pass / Fail shortcuts (window process).

Every platform backend has the same shape (``HotkeyBackend``): it registers
the two fixed combinations system-wide while enabled and only ever puts a
``HotkeyKind`` on the thread-safe queue it was given. The window drains that
queue on the Tk thread, so no backend touches a widget. Backends never raise
from ``enable`` / ``disable`` / ``close``: a failure becomes a ``focus_only``
status with a user-safe reason, and the window's own key bindings take over.
"""

import queue
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

HotkeyQueue = queue.Queue["HotkeyKind"]


class HotkeyKind(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class HotkeyState(StrEnum):
    # Registered system-wide: works while any app is in front.
    ACTIVE = "active"
    # Works only while the Test Runner window has focus (``reason`` says why).
    FOCUS_ONLY = "focus_only"
    # Turned off (CONTROLLER_MANUAL_SHORTCUTS) or not wanted now.
    OFF = "off"
    # Waiting for the desktop (e.g. its permission dialog).
    PENDING = "pending"


def default_triggers() -> dict[HotkeyKind, str]:
    """The fixed combinations as the tab shows them on this platform."""
    if sys.platform == "darwin":
        return {HotkeyKind.PASS: "⌘⇧P", HotkeyKind.FAIL: "⌘⇧F"}
    return {HotkeyKind.PASS: "Ctrl+Shift+P", HotkeyKind.FAIL: "Ctrl+Shift+F"}


@dataclass(frozen=True)
class HotkeyStatus:
    state: HotkeyState
    # User-safe; blank when active or off.
    reason: str = ""
    # The labels to show per kind (a portal may report its own triggers).
    triggers: Mapping[HotkeyKind, str] = field(default_factory=default_triggers)


OFF_STATUS = HotkeyStatus(state=HotkeyState.OFF)


def focus_only(reason: str) -> HotkeyStatus:
    return HotkeyStatus(state=HotkeyState.FOCUS_ONLY, reason=reason)


class HotkeyBackend(Protocol):
    """One platform's system-wide registration of the two combinations.

    ``enable`` registers (swallowing the combinations), ``disable`` releases
    them, ``close`` releases them for good and stops any thread. All three are
    idempotent, never raise and return quickly (slow work runs on the
    backend's own thread). ``status`` may change from another thread; it is
    always a complete ``HotkeyStatus`` (an immutable value).
    """

    @property
    def status(self) -> HotkeyStatus: ...

    def enable(self) -> None: ...

    def disable(self) -> None: ...

    def close(self) -> None: ...


class StaticBackend:
    """A backend that never registers anything (off, or focus-only)."""

    def __init__(self, status: HotkeyStatus) -> None:
        self._status = status

    @property
    def status(self) -> HotkeyStatus:
        return self._status

    def enable(self) -> None:
        return None

    def disable(self) -> None:
        return None

    def close(self) -> None:
        return None
