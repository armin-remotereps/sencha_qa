"""close_app and close_tab: close a desktop app, or its front tab, by name.

The platform modules do the work; this module validates the request, reads
the Test Runner's own processes (which are never closed) and times the reply.
Linux test machines are not supported.
"""

from __future__ import annotations

import sys
import time
from typing import Final

from controller_client.app_close_policy import (
    RunnerProcesses,
    current_runner_processes,
)
from controller_client.input_guard import ensure_input_not_blocked
from controller_client.protocol import (
    ActionResultPayload,
    CloseAppPayload,
    CloseTabPayload,
)

_CLOSE_APP_UNSUPPORTED: Final[str] = (
    "close_app is not supported on Linux test machines; use key_press alt+F4 "
    "with the app focused"
)
_CLOSE_TAB_UNSUPPORTED: Final[str] = (
    "close_tab is not supported on Linux test machines; use key_press ctrl+w "
    "(ctrl+shift+w in terminals) with the tab focused"
)

if sys.platform == "darwin":
    from controller_client.app_closer_macos import close_app as _close_app
    from controller_client.app_closer_macos import close_tab as _close_tab
elif sys.platform == "win32":
    from controller_client.app_closer_windows import close_app as _close_app
    from controller_client.app_closer_windows import close_tab as _close_tab
else:

    def _close_app(
        query: str, force: bool, runner: RunnerProcesses
    ) -> tuple[bool, str]:
        return False, _CLOSE_APP_UNSUPPORTED

    def _close_tab(query: str, runner: RunnerProcesses) -> tuple[bool, str]:
        return False, _CLOSE_TAB_UNSUPPORTED


def _result(success: bool, message: str, start: float) -> ActionResultPayload:
    return ActionResultPayload(
        success=success,
        message=message,
        duration_ms=(time.monotonic() - start) * 1000,
    )


def execute_close_app(payload: CloseAppPayload) -> ActionResultPayload:
    start = time.monotonic()
    query = payload.app_name.strip()
    if not query:
        return _result(False, "app_name must not be empty.", start)
    runner = current_runner_processes()
    success, message = _close_app(query, payload.force, runner)
    return _result(success, message, start)


def execute_close_tab(payload: CloseTabPayload) -> ActionResultPayload:
    start = time.monotonic()
    query = payload.app_name.strip()
    if not query:
        return _result(False, "app_name must not be empty.", start)
    # close_tab presses keys; fail before inspecting tabs when they would be
    # dropped (the key press checks again right before it is sent).
    ensure_input_not_blocked()
    runner = current_runner_processes()
    success, message = _close_tab(query, runner)
    return _result(success, message, start)
