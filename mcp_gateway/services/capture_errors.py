"""The refusals of ``list_windows`` and ``capture_screenshot``.

One table from readiness failures, the test machine's ``CaptureStatus`` and
the controller exceptions to ``McpToolError`` texts. Each text starts with a
stable ``[code]`` the client can branch on. The texts speak of "the test
machine" only: never the program that runs on it, an agent or an AI (the no
trace rule); runner details pass through ``_safe_detail`` for that reason.
Window titles never appear here; desktop names are quoted as data.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from controller_client.protocol import CaptureStatus
from knowledge_base.tool_text import one_line, quoted_name
from mcp_gateway.exceptions import McpToolError
from projects.controller_capture_actions import WindowDesktop
from projects.exceptions import (
    ControllerActionError,
    ControllerNotConnectedError,
    DeviceDisconnectedError,
    TestRunnerUnresponsiveError,
)
from projects.runner_capture_readiness import CaptureReadiness


class CaptureErrorCode(StrEnum):
    NOT_SET_UP = "not_set_up"
    MACHINE_OFFLINE = "machine_offline"
    MACHINE_OUTDATED = "machine_outdated"
    DEVICE_OFFLINE = "device_offline"
    MACHINE_BUSY = "machine_busy"
    MACHINE_UNRESPONSIVE = "machine_unresponsive"
    NOT_ALLOWED = "not_allowed"
    WINDOW_NOT_FOUND = "window_not_found"
    WINDOW_MINIMIZED = "window_minimized"
    WINDOW_ON_OTHER_DESKTOP = "window_on_other_desktop"
    DESKTOP_SWITCH_FAILED = "desktop_switch_failed"
    WINDOW_NOT_SETTLED = "window_not_settled"
    WINDOW_OBSCURED = "window_obscured"
    REGION_OUT_OF_BOUNDS = "region_out_of_bounds"
    SCREEN_NOT_CLEAN = "screen_not_clean"
    NOT_INTERACTIVE = "not_interactive"
    PERMISSION_MISSING = "permission_missing"
    COMPOSITOR_UNSUPPORTED = "compositor_unsupported"
    HELPER_MISSING = "helper_missing"
    CAPTURE_BLANK = "capture_blank"
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"
    CAPTURE_FAILED = "capture_failed"


_MEANWHILE: Final = (
    " Meanwhile, capture the screenshot yourself and send it with add_screenshot."
)
_IF_IT_KEEPS: Final = (
    " If it keeps failing, capture the screenshot yourself and send it with "
    "add_screenshot."
)
_FIXED: Final[dict[CaptureErrorCode, str]] = {
    CaptureErrorCode.NOT_SET_UP: (
        "No applications are allowed for screenshot capture on the test machine "
        "in this project. Capture the screenshot yourself and send it with "
        "add_screenshot."
    ),
    CaptureErrorCode.MACHINE_OFFLINE: (
        "The test machine is not connected right now. Try again later." + _MEANWHILE
    ),
    CaptureErrorCode.MACHINE_OUTDATED: (
        "The test machine's software is too old to capture screenshots. Ask a "
        "project member to update the test machine's software from the "
        "Environment page." + _MEANWHILE
    ),
    CaptureErrorCode.DEVICE_OFFLINE: (
        "The phone or tablet of the test machine is not connected right now. "
        "Try again later."
    ),
    CaptureErrorCode.MACHINE_BUSY: (
        "The test machine is taking another screenshot. Try again in a few seconds."
    ),
    CaptureErrorCode.MACHINE_UNRESPONSIVE: (
        "The test machine did not answer in time. Try again." + _IF_IT_KEEPS
    ),
    CaptureErrorCode.WINDOW_NOT_FOUND: (
        "No allowed window matches. Call list_windows and pass the window_id of "
        "the window you want."
    ),
    CaptureErrorCode.WINDOW_MINIMIZED: (
        "The window is minimised. Restore it and call again."
    ),
    CaptureErrorCode.WINDOW_NOT_SETTLED: (
        "The window kept changing after the desktop switch; call again with a "
        "larger delay_ms."
    ),
    CaptureErrorCode.WINDOW_OBSCURED: (
        "Another window covers it; bring it to the front and call again."
    ),
    CaptureErrorCode.CAPTURE_BLANK: (
        "The captured image was blank (all black or a single colour). Make sure "
        "the window is shown and the screen is not locked, then call again."
    ),
    CaptureErrorCode.TOO_LARGE: (
        "The capture is too large even after downscaling. Capture a window or a "
        "region of it instead."
    ),
    CaptureErrorCode.UNSUPPORTED: (
        "The test machine cannot capture this target. A phone or tablet takes "
        "target screen or region only."
    ),
    CaptureErrorCode.INVALID: "The test machine refused the request as invalid.",
    CaptureErrorCode.CAPTURE_FAILED: (
        "The test machine could not complete the request. Try again." + _IF_IT_KEEPS
    ),
}
# The runner's fixed explanation follows these.
_WITH_DETAIL: Final[dict[CaptureErrorCode, str]] = {
    CaptureErrorCode.NOT_INTERACTIVE: (
        "The test machine has no logged-in, unlocked session that can be captured"
    ),
    CaptureErrorCode.PERMISSION_MISSING: (
        "The test machine lacks the permission to capture the screen"
    ),
    CaptureErrorCode.COMPOSITOR_UNSUPPORTED: (
        "The test machine's desktop does not support window capture"
    ),
    CaptureErrorCode.HELPER_MISSING: (
        "A capture component is missing on the test machine"
    ),
}
_RUNNER_NAME: Final = re.compile(r"\b(?:the\s+)?test\s+runner\b", re.IGNORECASE)
_SHOT_SIZE: Final = re.compile(r"\d{1,5}x\d{1,5}")
_PROCESS_LIST: Final = re.compile(r"[a-z0-9 ._+,-]{1,300}")


def error(code: CaptureErrorCode, text: str | None = None) -> McpToolError:
    """The tool error ``[code] text`` (the table's text when None)."""
    return McpToolError(f"[{code.value}] {text if text is not None else _FIXED[code]}")


def readiness_code(readiness: CaptureReadiness) -> CaptureErrorCode | None:
    """Why runner capture is unavailable now, or None when it is available."""
    if not readiness.allowed:
        return CaptureErrorCode.NOT_SET_UP
    if not readiness.connected:
        return CaptureErrorCode.MACHINE_OFFLINE
    if not readiness.supported:
        return CaptureErrorCode.MACHINE_OUTDATED
    if readiness.phone and not readiness.device_ready:
        return CaptureErrorCode.DEVICE_OFFLINE
    return None


def refuse_unready(readiness: CaptureReadiness) -> None:
    code = readiness_code(readiness)
    if code is not None:
        raise error(code)


def not_allowed(process: str, allowed: tuple[str, ...]) -> McpToolError:
    """``process`` (a normalized name, or "") is not on the allowlist."""
    which = (
        f"That window belongs to {process}, which is not allowed for capture."
        if process
        else "That window's application is not allowed for capture."
    )
    return error(
        CaptureErrorCode.NOT_ALLOWED,
        f"{which} Allowed applications: {', '.join(allowed)}.",
    )


def controller_error(exc: Exception) -> McpToolError:
    """A controller exception of a list or capture round trip."""
    if isinstance(exc, ControllerNotConnectedError):
        return error(CaptureErrorCode.MACHINE_OFFLINE)
    if isinstance(exc, DeviceDisconnectedError):
        return error(CaptureErrorCode.DEVICE_OFFLINE)
    if isinstance(exc, TestRunnerUnresponsiveError):
        return error(CaptureErrorCode.MACHINE_UNRESPONSIVE)
    if isinstance(exc, ControllerActionError):
        return error(CaptureErrorCode.CAPTURE_FAILED)
    raise TypeError(f"not a controller exception: {type(exc).__name__}")


def status_error(
    status: CaptureStatus,
    detail: str,
    *,
    allowed: tuple[str, ...],
    process: str = "",
    desktop: WindowDesktop | None = None,
    desktop_restored: bool | None = None,
) -> McpToolError:
    """The tool error of a refused ``list_windows`` / ``capture_window``
    (``status`` is not OK)."""
    restore_note = (
        " The test machine also could not switch back to the desktop that was "
        "shown before; switch back yourself."
        if desktop_restored is False
        else ""
    )
    if status is CaptureStatus.NOT_ALLOWED:
        exc = not_allowed(process, allowed)
        return McpToolError(exc.message + restore_note)
    if status is CaptureStatus.WINDOW_ON_OTHER_DESKTOP:
        return error(
            CaptureErrorCode.WINDOW_ON_OTHER_DESKTOP,
            f"The window is on {_desktop_text(desktop)}. Switch to it and call again.",
        )
    if status is CaptureStatus.DESKTOP_SWITCH_FAILED:
        back = " and could not switch back" if desktop_restored is False else ""
        safe = _safe_detail(detail)
        return error(
            CaptureErrorCode.DESKTOP_SWITCH_FAILED,
            f"The test machine could not switch to {_desktop_text(desktop)}{back}"
            + (f": {safe}" if safe else "")
            + ".",
        )
    if status is CaptureStatus.REGION_OUT_OF_BOUNDS:
        size = f" ({detail} pixels)" if _SHOT_SIZE.fullmatch(detail.strip()) else ""
        return error(
            CaptureErrorCode.REGION_OUT_OF_BOUNDS,
            f"The region lies outside the window's shot{size}. Pass x, y, w and h "
            "inside it, in pixels relative to the window." + restore_note,
        )
    if status is CaptureStatus.SCREEN_NOT_CLEAN:
        names = detail.strip()
        shown = f" ({names})" if _PROCESS_LIST.fullmatch(names) else ""
        return error(
            CaptureErrorCode.SCREEN_NOT_CLEAN,
            f"Other applications are visible on that screen{shown}. Close or move "
            "them, or capture target window instead." + restore_note,
        )
    code = _STATUS_CODES.get(status, CaptureErrorCode.CAPTURE_FAILED)
    lead = _WITH_DETAIL.get(code)
    if lead is not None:
        safe = _safe_detail(detail)
        return error(code, lead + (f": {safe}" if safe else "") + "." + restore_note)
    return error(code, _FIXED[code] + restore_note)


_STATUS_CODES: Final[dict[CaptureStatus, CaptureErrorCode]] = {
    CaptureStatus.WINDOW_NOT_FOUND: CaptureErrorCode.WINDOW_NOT_FOUND,
    CaptureStatus.WINDOW_MINIMIZED: CaptureErrorCode.WINDOW_MINIMIZED,
    CaptureStatus.WINDOW_NOT_SETTLED: CaptureErrorCode.WINDOW_NOT_SETTLED,
    CaptureStatus.WINDOW_OBSCURED: CaptureErrorCode.WINDOW_OBSCURED,
    CaptureStatus.NOT_INTERACTIVE: CaptureErrorCode.NOT_INTERACTIVE,
    CaptureStatus.PERMISSION_MISSING: CaptureErrorCode.PERMISSION_MISSING,
    CaptureStatus.COMPOSITOR_UNSUPPORTED: CaptureErrorCode.COMPOSITOR_UNSUPPORTED,
    CaptureStatus.HELPER_MISSING: CaptureErrorCode.HELPER_MISSING,
    CaptureStatus.CAPTURE_BLANK: CaptureErrorCode.CAPTURE_BLANK,
    CaptureStatus.TOO_LARGE: CaptureErrorCode.TOO_LARGE,
    CaptureStatus.UNSUPPORTED: CaptureErrorCode.UNSUPPORTED,
    CaptureStatus.INVALID: CaptureErrorCode.INVALID,
    # The screen could not be prepared for the capture: no reason to name.
    CaptureStatus.HIDE_FAILED: CaptureErrorCode.CAPTURE_FAILED,
    CaptureStatus.CAPTURE_FAILED: CaptureErrorCode.CAPTURE_FAILED,
}


def _desktop_text(desktop: WindowDesktop | None) -> str:
    """ "desktop 3 ("Testing")", "desktop 3", "the desktop "Testing"" or
    "another desktop"; the name quoted as data."""
    if desktop is None:
        return "another desktop"
    name = quoted_name(desktop.name) if desktop.name and desktop.name.strip() else ""
    if desktop.index is not None:
        return f"desktop {desktop.index}" + (f" ({name})" if name else "")
    return f"the desktop {name}" if name else "another desktop"


def _safe_detail(detail: str) -> str:
    """The runner's explanation on one line, the program's name replaced
    (it never carries a window title)."""
    text = _RUNNER_NAME.sub("the test machine's software", one_line(detail))
    return text.rstrip(". ")
