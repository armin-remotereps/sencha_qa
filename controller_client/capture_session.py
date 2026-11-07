"""Whether this session can show and capture windows right now.

Checked
before every ``list_windows`` and every ``capture_window``; delegates to the
platform's ``SESSION`` (``capture_platform.CaptureSessionBackend``):
Windows session 0, a locked or secure desktop, a disconnected remote session;
macOS Screen Recording or a locked screen; Linux without a display or session
bus, a locked screen, KDE Wayland or an unknown compositor.
"""

from __future__ import annotations

import logging
import sys

from controller_client.capture_platform import UNSUPPORTED_OS_DETAIL
from controller_client.protocol import CaptureStatus

if sys.platform == "win32":
    from controller_client.capture_session_windows import SESSION
elif sys.platform == "darwin":
    from controller_client.capture_session_macos import SESSION
elif sys.platform.startswith("linux"):
    from controller_client.capture_session_linux import SESSION

logger = logging.getLogger(__name__)


if sys.platform in ("win32", "darwin") or sys.platform.startswith("linux"):

    def _platform_problem() -> tuple[CaptureStatus, str] | None:
        return SESSION.problem()

else:

    def _platform_problem() -> tuple[CaptureStatus, str] | None:
        return CaptureStatus.UNSUPPORTED, UNSUPPORTED_OS_DETAIL


def interactive_session_problem() -> tuple[CaptureStatus, str] | None:
    """None when listing and capturing can go ahead, else the status and its
    fixed, user-safe detail. Quick and blocking (call it from a worker
    thread); never raises. The rule on every platform: a failing check (an
    internal error, not a detected problem) is logged as a warning and lets
    the capture go on, whose blank check still refuses an empty frame."""
    try:
        return _platform_problem()
    except Exception:
        logger.warning("The capture session check failed", exc_info=True)
        return None
