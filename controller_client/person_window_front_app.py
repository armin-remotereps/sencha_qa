"""macOS: give the front back to the app the person-step window took it from.

Tk activates the window process's (Python) app whenever it shows a window,
and the app stays active after the window is withdrawn, so the agent's next
key press would go to an app with no window. The window process records the
frontmost app right before it shows and, whenever it hides (agent action,
answer, close), re-activates that app and waits until it is front, before the
hide is acknowledged. A no-op on other platforms and when pyobjc's AppKit is
missing.
"""

import logging
import os
import sys
import time
from typing import Final

logger = logging.getLogger(__name__)

# How long a hide waits for the recorded app to become front.
RESTORE_TIMEOUT_SECONDS: Final[float] = 0.6

if sys.platform == "darwin":
    try:
        import AppKit
    except ImportError as _e:  # pyobjc comes with PyAutoGUI; guard anyway
        logger.warning("Cannot restore the front app after hiding: %s", _e)
        _HAVE_APPKIT = False
    else:
        _HAVE_APPKIT = True

    class FrontAppKeeper:
        """Records the front app before a show; re-activates it on a hide."""

        def __init__(self) -> None:
            self._own_pid = os.getpid()
            self._previous: AppKit.NSRunningApplication | None = None
            self._deadline = 0.0

        def _front(self) -> "AppKit.NSRunningApplication | None":
            return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()

        def before_show(self) -> None:
            if not _HAVE_APPKIT:
                return
            front = self._front()
            # Already front (the window was showing or the person clicked
            # it): keep the app recorded earlier.
            if front is not None and front.processIdentifier() != self._own_pid:
                self._previous = front

        def start_restore(self) -> bool:
            """Ask the recorded app to come front; True when it was asked.

            Only when this process is the front app (otherwise the person
            or the system already moved on) and the app is still running.
            """
            if not _HAVE_APPKIT:
                return False
            previous = self._previous
            front = self._front()
            if (
                previous is None
                or front is None
                or front.processIdentifier() != self._own_pid
                or previous.isTerminated()
            ):
                return False
            # IgnoringOtherApps is deprecated (no effect) on macOS 14+, where
            # activation is cooperative: this process is active, so it may
            # hand the front over.
            if not previous.activateWithOptions_(
                AppKit.NSApplicationActivateIgnoringOtherApps
            ):
                logger.warning(
                    "Could not bring %s back to the front", previous.localizedName()
                )
                return False
            self._deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
            return True

        def restored(self) -> bool:
            """True once the recorded app is front, or the wait timed out.

            The front app updates through the main run loop: call this from
            Tk callbacks (``after``), not in a blocking loop.
            """
            previous = self._previous
            front = self._front()
            if (
                previous is None
                or front is None
                or front.processIdentifier() == previous.processIdentifier()
            ):
                return True
            if time.monotonic() >= self._deadline:
                logger.warning(
                    "%s did not come back to the front within %.1f s",
                    previous.localizedName(),
                    RESTORE_TIMEOUT_SECONDS,
                )
                return True
            return False

else:

    class FrontAppKeeper:
        """Nothing to restore: showing does not activate the window here."""

        def before_show(self) -> None:
            return None

        def start_restore(self) -> bool:
            return False

        def restored(self) -> bool:
            return True
