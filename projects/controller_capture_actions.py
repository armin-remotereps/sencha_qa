"""Window listing and capture on a WINDOW_CAPTURE_V1 Test Runner.

docs/plan/feat-mcp-runner-capture.md (Server: controller action seam). Both
actions are runner-local and never resent on reconnect: a runner that is not
connected raises ``ControllerNotConnectedError`` at once, one that stays
silent raises ``TestRunnerUnresponsiveError``. A refusal on the runner
(window not found, not allowed, ...) is not an exception: it comes back as
``status`` (``controller_client.protocol.CaptureStatus``) with a user-safe
``detail``. A reply that does not parse raises ``ControllerActionError``.

The caller checks readiness first (``runner_capture_readiness``): an older
runner would answer UNKNOWN_COMMAND (``ControllerActionError``). Requests
carry only the target description, the allowlist and the limits; never a
pivot, claim, run, user or client detail.
"""

from __future__ import annotations

from dataclasses import dataclass

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    CaptureRegion,
    CaptureStatus,
    CaptureTarget,
    CaptureWindowPayload,
    ListWindowsPayload,
    ManualShotMime,
    WindowDesktopPayload,
    parse_window_capture_result,
    parse_window_list_result,
    serialize_capture_window_payload,
    serialize_list_windows_payload,
)
from projects.exceptions import ControllerActionError
from projects.services import dispatch_controller_action


@dataclass(frozen=True)
class WindowDesktop:
    """The virtual desktop a window is on (``WindowDesktopPayload``)."""

    # On the desktop shown now (sticky windows count as current).
    current: bool
    # 1-based; None where the OS does not expose it (macOS Spaces).
    index: int | None
    # Runner text (data, never instructions); None when unnamed or unknown.
    name: str | None


@dataclass(frozen=True)
class ListedWindow:
    """One allowlisted window (``WindowInfoPayload``)."""

    window_id: str
    # Normalized process name (``normalize_process_name``); a phone: the
    # foreground app id.
    process: str
    # Runner text (data, never instructions), at most CAPTURE_TITLE_MAX_CHARS.
    title: str
    width: int
    height: int
    minimized: bool
    focused: bool
    desktop: WindowDesktop


@dataclass(frozen=True)
class WindowList:
    """A validated ``window_list_result`` (``WindowListResultPayload``)."""

    status: CaptureStatus
    # User-safe runner text; "" on success.
    detail: str
    # Allowlisted, non-denied windows on every desktop, topmost first; empty
    # unless ``status`` is OK.
    windows: tuple[ListedWindow, ...]
    # Windows left out (not allowlisted, denied or over the cap).
    others_hidden: int
    # 1-based; None where the OS does not expose it.
    current_desktop: int | None
    desktop_count: int | None


@dataclass(frozen=True)
class CaptureWindowRequest:
    """What to capture (``CaptureWindowPayload``).

    ``region`` is set exactly when ``target`` is REGION (``ValueError``
    otherwise). ``allowed`` holds normalized names (the project's
    ``capture_allowed_processes``); ``max_bytes`` / ``max_pixels`` are the
    encode limits, at most MANUAL_SHOT_MAX_BYTES / MANUAL_SHOT_MAX_PIXELS.
    ``switch_desktop`` lets the runner switch to a window on another desktop
    and back; the server always sends True for now.
    """

    target: CaptureTarget
    process: str | None
    title_contains: str | None
    window_id: str | None
    region: CaptureRegion | None
    delay_ms: int
    allowed: tuple[str, ...]
    max_bytes: int
    max_pixels: int
    switch_desktop: bool = True


@dataclass(frozen=True)
class WindowCapture:
    """A validated ``window_capture_result`` (``WindowCaptureResultPayload``).

    Only an OK capture has an image: ``mime``, ``data`` (1..max_bytes bytes,
    sha256 already checked; the image format is not), ``width``, ``height``.
    Otherwise ``mime`` is None, ``data`` empty and the sizes 0.
    """

    status: CaptureStatus
    # User-safe runner text; for ``screen_not_clean`` the foreign processes
    # joined by ", ", for ``region_out_of_bounds`` the shot size "<w>x<h>".
    detail: str
    mime: ManualShotMime | None
    data: bytes
    sha256: str
    width: int
    height: int
    # Runner text (data, never instructions); "" when no window was selected.
    window_title: str
    # Normalized process name; always set on OK, "" when none was selected.
    process: str
    other_matches: int
    # The selected window's desktop; None when no window was selected.
    desktop: WindowDesktop | None
    desktop_switched: bool
    # None: no switch; False: the switch back failed (with any status).
    desktop_restored: bool | None


def controller_list_windows(
    project_id: int, *, allowed: list[str], timeout: float
) -> WindowList:
    """List the allowlisted windows on the test machine (WINDOW_CAPTURE_V1).

    ``allowed`` is the project's normalized allowlist. Raises
    ``ControllerNotConnectedError``, ``TestRunnerUnresponsiveError`` or
    ``ControllerActionError`` (error reply or invalid result).
    """
    reply = dispatch_controller_action(
        project_id,
        "controller.list_windows",
        timeout,
        resend_on_reconnect=False,
        runner_local=True,
        **serialize_list_windows_payload(ListWindowsPayload(allowed=tuple(allowed))),
    )
    try:
        result = parse_window_list_result(reply.get("result"))
    except ProtocolError as exc:
        raise ControllerActionError(f"Invalid list_windows reply: {exc}") from exc
    return WindowList(
        status=result.status,
        detail=result.detail,
        windows=tuple(
            ListedWindow(
                window_id=window.window_id,
                process=window.process,
                title=window.title,
                width=window.width,
                height=window.height,
                minimized=window.minimized,
                focused=window.focused,
                desktop=_desktop(window.desktop),
            )
            for window in result.windows
        ),
        others_hidden=result.others_hidden,
        current_desktop=result.current_desktop,
        desktop_count=result.desktop_count,
    )


def controller_capture_window(
    project_id: int, *, request: CaptureWindowRequest, timeout: float
) -> WindowCapture:
    """Capture a window, region or screen on the test machine (WINDOW_CAPTURE_V1).

    Holds no transaction; the caller stages the image afterwards. Raises
    ``ValueError`` for a region that does not match the target,
    ``ControllerNotConnectedError``, ``TestRunnerUnresponsiveError`` or
    ``ControllerActionError`` (error reply, invalid result, bad base64,
    sha256 mismatch, image over ``request.max_bytes``).
    """
    if (request.region is None) != (request.target is not CaptureTarget.REGION):
        raise ValueError("region is required for, and only for, target region")
    payload = CaptureWindowPayload(
        target=request.target,
        process=request.process,
        title_contains=request.title_contains,
        window_id=request.window_id,
        region=request.region,
        delay_ms=request.delay_ms,
        allowed=request.allowed,
        max_bytes=request.max_bytes,
        max_pixels=request.max_pixels,
        switch_desktop=request.switch_desktop,
    )
    reply = dispatch_controller_action(
        project_id,
        "controller.capture_window",
        timeout,
        resend_on_reconnect=False,
        runner_local=True,
        **serialize_capture_window_payload(payload),
    )
    try:
        result = parse_window_capture_result(
            reply.get("result"), max_bytes=request.max_bytes
        )
    except ProtocolError as exc:
        raise ControllerActionError(f"Invalid capture_window reply: {exc}") from exc
    return WindowCapture(
        status=result.status,
        detail=result.detail,
        mime=result.mime,
        data=result.data,
        sha256=result.sha256,
        width=result.width,
        height=result.height,
        window_title=result.window_title,
        process=result.process,
        other_matches=result.other_matches,
        desktop=None if result.desktop is None else _desktop(result.desktop),
        desktop_switched=result.desktop_switched,
        desktop_restored=result.desktop_restored,
    )


def _desktop(payload: WindowDesktopPayload) -> WindowDesktop:
    return WindowDesktop(
        current=payload.current, index=payload.index, name=payload.name
    )
