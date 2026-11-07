"""Session check for the window capture on macOS.

Exports ``SESSION`` (``capture_platform.CaptureSessionBackend``).

* No console session, the session switched away (fast user switching) or the
  screen locked (``CGSessionCopyCurrentDictionary``): ``not_interactive``.
* Screen Recording not granted (``CGPreflightScreenCaptureAccess``, which
  never prompts): ``permission_missing`` with ``MACOS_PERMISSION_HINT``.
  Without it ``screencapture`` returns the wallpaper only and window titles
  are withheld, so nothing may be listed or captured.

A check that fails internally (an exception, not a detected problem) is
logged and lets the capture go on, as on every platform
(``capture_session``).
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    import logging
    from collections.abc import Mapping
    from typing import Final

    import Quartz

    from controller_client.capture_platform import CaptureSessionBackend
    from controller_client.protocol import CaptureStatus
    from controller_client.screen_capture_macos import MACOS_PERMISSION_HINT

    logger = logging.getLogger(__name__)

    # Keys of the CGSession dictionary (documented in CGSession.h, except the
    # lock flag, which the window server sets while the lock screen shows).
    _ON_CONSOLE_KEY: Final[str] = "kCGSSessionOnConsoleKey"
    _LOGIN_DONE_KEY: Final[str] = "kCGSessionLoginDoneKey"
    _SCREEN_LOCKED_KEY: Final[str] = "CGSSessionScreenIsLocked"

    _NO_SESSION: Final[str] = (
        "the Test Runner is not running inside a logged-in desktop session"
    )
    _NOT_ON_CONSOLE: Final[str] = (
        "the test machine's session is not the one on screen (another user is "
        "switched in); switch back to it"
    )
    _LOCKED: Final[str] = "the test machine's screen is locked; unlock it"

    def _flag(session: Mapping[str, object], key: str) -> bool | None:
        """A boolean entry of the session dictionary; None when absent."""
        value = session.get(key)
        if isinstance(value, (bool, int)):
            return bool(value)
        return None

    class _MacSession:
        def problem(self) -> tuple[CaptureStatus, str] | None:
            try:
                session = Quartz.CGSessionCopyCurrentDictionary()
                if session is None:
                    return CaptureStatus.NOT_INTERACTIVE, _NO_SESSION
                if _flag(session, _ON_CONSOLE_KEY) is False:
                    return CaptureStatus.NOT_INTERACTIVE, _NOT_ON_CONSOLE
                if _flag(session, _LOGIN_DONE_KEY) is False:
                    return CaptureStatus.NOT_INTERACTIVE, _NO_SESSION
                if _flag(session, _SCREEN_LOCKED_KEY):
                    return CaptureStatus.NOT_INTERACTIVE, _LOCKED
                if not Quartz.CGPreflightScreenCaptureAccess():
                    return CaptureStatus.PERMISSION_MISSING, MACOS_PERMISSION_HINT
            except Exception:  # noqa: BLE001 - the session check must never raise
                # The core rule (capture_session): an internal error lets the
                # capture go on; its blank check still refuses an empty frame.
                logger.warning("Could not read the macOS session state", exc_info=True)
                return None
            return None

    SESSION: CaptureSessionBackend = _MacSession()
