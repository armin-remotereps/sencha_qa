"""One process-wide lock for everything that captures the screen.

Server messages are already handled one at a time (``_receive_messages`` awaits each
``_dispatch``), so no agent action runs while a ``capture_window`` switches
desktops. Captures that start from the person window (tester screenshots and
the Manual run shortcuts, ``ManualCapturer``) run in their own tasks, so they
and ``WindowCaptureService`` take this lock: two screen captures, hides and
desktop switches never interleave.

``asyncio.Lock`` binds to the running loop on first contended use, so the one
instance serves the runner's loop (and a diagnostic's own loop, which never
contends).
"""

from __future__ import annotations

import asyncio

_SCREEN_USE_LOCK = asyncio.Lock()


def screen_use_lock() -> asyncio.Lock:
    """The lock every screen capture holds while it hides, switches and grabs."""
    return _SCREEN_USE_LOCK
