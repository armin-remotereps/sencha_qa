"""Use case: the test machine captures a screenshot for the MCP tester's
claimed case (capture_screenshot).

1. The claim (no lock) and not paused; the staged-shot caps are checked
   before anything is captured.
2. Capture readiness (allowlist, connection, capability, phone device) and
   the request against it: a desktop runner needs ``window_match`` for
   window and region; a named process must be allowlisted.
3. A process round-trip slot and one capture per project at a time
   (``capture_lock``, both released in ``finally``) for the runner round
   trip, which holds no transaction.
4. A refusal on the machine becomes a coded tool error
   (``capture_errors``); a success is checked again: its process must be on
   the allowlist (refused and logged with the process name only).
5. ``stage_shot(capture_source="runner")`` stages it like an
   ``add_screenshot`` shot (same image check and caps, the claim checked
   again under the row lock, so a claim that ended meanwhile gets nothing).
   Its decode slot is waited for only until MCP_CAPTURE_DEADLINE_SECONDS
   after the call started (``[machine_busy]``, nothing staged), so the
   client never misses the shot_id of a staged shot.
6. A JPEG preview (never stored), built from the same decode, goes back
   with the structured result.

The request to the machine carries only the target, the allowlist and the
limits: nothing about the claim, the case or the caller. A success whose
switch back to the previous desktop failed is still staged; the response
flags it (``desktop_restored`` false) so the client can switch back.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Final

from django.conf import settings

from controller_client.protocol import (
    MANUAL_SHOT_MAX_PIXELS,
    CaptureRegion,
    CaptureStatus,
    CaptureTarget,
    normalize_process_name,
)
from mcp_gateway.exceptions import DecodeSlotTimeout
from mcp_gateway.limits import (
    MCP_CAPTURE_DEADLINE_SECONDS,
    MCP_CAPTURE_REPLY_TIMEOUT_SECONDS,
)
from mcp_gateway.schemas import CaptureScreenshotRequest, CaptureScreenshotResponse
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.capture_errors import (
    CaptureErrorCode,
    controller_error,
    error,
    not_allowed,
    refuse_unready,
    status_error,
)
from mcp_gateway.services.capture_lock import capture_lock, machine_round_trip
from mcp_gateway.services.capture_mapping import desktop_item
from mcp_gateway.services.own_claim import load_own_claim, refuse_paused
from mcp_gateway.services.stage_shot import refuse_full, stage_shot
from mcp_gateway.untrusted_text import case_line
from projects.controller_capture_actions import (
    CaptureWindowRequest,
    WindowCapture,
    controller_capture_window,
)
from projects.exceptions import ControllerActionError, ControllerNotConnectedError
from projects.models import ScreenshotCaptureSource
from projects.runner_capture_readiness import CaptureReadiness, capture_readiness

logger = logging.getLogger(__name__)

# Appended to an image or storage refusal of a captured shot.
_CAPTURE_HINT: Final = " Capture a window or a region of it instead."
_NEEDS_MATCH: Final = (
    "window_match is required for target window and region: pass the "
    "window_id from list_windows, or process and/or title_contains."
)
_BAD_PROCESS: Final = "window_match.process is not a valid process name."


@dataclass(frozen=True)
class CaptureOutcome:
    """The structured result and the preview image (JPEG bytes)."""

    response: CaptureScreenshotResponse
    preview_jpeg: bytes


def capture_screenshot(
    call: McpCall, request: CaptureScreenshotRequest
) -> CaptureOutcome:
    deadline = time.monotonic() + MCP_CAPTURE_DEADLINE_SECONDS
    project_id = call.project.pk
    refuse_paused(
        load_own_claim(
            project_id=project_id,
            pivot_id=request.pivot_id,
            claim_token=request.claim_token,
            lock=False,
        )
    )
    # A full claim is refused before the machine does any work.
    refuse_full(
        pivot_id=request.pivot_id,
        claim_token=request.claim_token,
        adding=0,
        hint=_CAPTURE_HINT,
    )
    readiness = capture_readiness(project_id)
    refuse_unready(readiness)
    window_request = _window_request(request, readiness)

    capture = _capture_once(project_id, window_request)
    if capture.desktop_restored is False:
        logger.warning(
            "capture on project %s could not switch the desktop back", project_id
        )
    if capture.status is not CaptureStatus.OK:
        raise status_error(
            capture.status,
            capture.detail,
            allowed=readiness.allowed,
            process=capture.process,
            desktop=capture.desktop,
            desktop_restored=capture.desktop_restored,
        )
    if capture.mime is None:
        # The parser guarantees an image on OK; never stage without one.
        raise error(CaptureErrorCode.CAPTURE_FAILED)
    if normalize_process_name(capture.process) not in readiness.allowed:
        logger.warning(
            "capture on project %s refused: process %r is not allowlisted",
            project_id,
            capture.process,
        )
        raise status_error(
            CaptureStatus.NOT_ALLOWED,
            "",
            allowed=readiness.allowed,
            process=capture.process,
            desktop_restored=capture.desktop_restored,
        )

    try:
        staged = stage_shot(
            call,
            pivot_id=request.pivot_id,
            claim_token=request.claim_token,
            mime=capture.mime,
            data=capture.data,
            capture_source=ScreenshotCaptureSource.RUNNER.value,
            hint=_CAPTURE_HINT,
            preview=True,
            deadline=deadline,
        )
    except DecodeSlotTimeout as exc:
        raise error(CaptureErrorCode.MACHINE_BUSY) from exc
    if staged.preview_jpeg is None:
        # stage_shot builds it when preview=True; never answer without one.
        raise error(CaptureErrorCode.CAPTURE_FAILED)
    return CaptureOutcome(
        response=CaptureScreenshotResponse(
            shot_id=staged.shot_id,
            width=staged.width,
            height=staged.height,
            bytes=staged.size,
            mime=capture.mime.value,
            window_title=case_line(capture.window_title),
            process=capture.process,
            target=window_request.target.value,
            other_matches=capture.other_matches,
            desktop=None if capture.desktop is None else desktop_item(capture.desktop),
            desktop_switched=capture.desktop_switched,
            desktop_restored=capture.desktop_restored,
        ),
        preview_jpeg=staged.preview_jpeg,
    )


def _window_request(
    request: CaptureScreenshotRequest, readiness: CaptureReadiness
) -> CaptureWindowRequest:
    """The machine request; refuses what the machine would refuse anyway."""
    target = CaptureTarget(request.target)
    match = (
        request.window_match
        if request.window_match is not None and not request.window_match.empty
        else None
    )
    if match is None and target is not CaptureTarget.SCREEN and not readiness.phone:
        raise error(CaptureErrorCode.INVALID, _NEEDS_MATCH)
    process: str | None = None
    if match is not None and match.process is not None:
        process = normalize_process_name(match.process)
        if process is None:
            raise error(CaptureErrorCode.INVALID, _BAD_PROCESS)
        if process not in readiness.allowed:
            raise not_allowed(process, readiness.allowed)
    region = request.region
    return CaptureWindowRequest(
        target=target,
        process=process,
        title_contains=match.title_contains if match is not None else None,
        window_id=match.window_id if match is not None else None,
        region=(
            None
            if region is None
            else CaptureRegion(x=region.x, y=region.y, w=region.w, h=region.h)
        ),
        delay_ms=request.delay_ms,
        allowed=readiness.allowed,
        max_bytes=settings.MANUAL_SHOT_MAX_BYTES,
        max_pixels=MANUAL_SHOT_MAX_PIXELS,
        switch_desktop=True,
    )


def _capture_once(
    project_id: int, window_request: CaptureWindowRequest
) -> WindowCapture:
    """The runner round trip under a process round-trip slot and the
    project's capture lock (no transaction is open here)."""
    with machine_round_trip(), capture_lock(project_id):
        try:
            return controller_capture_window(
                project_id,
                request=window_request,
                timeout=MCP_CAPTURE_REPLY_TIMEOUT_SECONDS,
            )
        except (ControllerNotConnectedError, ControllerActionError) as exc:
            raise controller_error(exc) from exc
