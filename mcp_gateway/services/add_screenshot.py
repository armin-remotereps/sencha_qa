"""Use case: the MCP tester sends one screenshot of its claimed case.

The client captured the image itself (the window, screen or region it
chose); the server never crops, picks a window or downscales it. It checks
the claim, the base64 length before decoding, decodes strictly and stages
the bytes (``stage_shot``: the manual screenshot check, then the per-claim
caps under the case row lock). A later ``mark_step`` binds it to a step.
"""

from __future__ import annotations

import base64
import binascii
from typing import Final

from controller_client.protocol import ManualShotMime
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.limits import MCP_SHOT_MAX_BASE64_CHARS
from mcp_gateway.schemas import AddScreenshotRequest, AddScreenshotResponse
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.own_claim import load_own_claim, refuse_paused
from mcp_gateway.services.stage_shot import SMALLER_HINT, stage_shot
from projects.models import ScreenshotCaptureSource

_TOO_LARGE: Final = "The image is larger than the server accepts." + SMALLER_HINT
_NOT_BASE64: Final = (
    "image_base64 is not valid base64 (standard alphabet, padded, no data: "
    "prefix, no line breaks)."
)
_EMPTY: Final = "The image is empty."


def _decode(image_base64: str) -> bytes:
    """Strict base64 decode, refused by length before any decoding."""
    if len(image_base64) > MCP_SHOT_MAX_BASE64_CHARS:
        raise McpToolError(_TOO_LARGE)
    try:
        data = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise McpToolError(_NOT_BASE64) from exc
    if not data:
        raise McpToolError(_EMPTY)
    return data


def add_screenshot(
    call: McpCall, request: AddScreenshotRequest
) -> AddScreenshotResponse:
    # Cheap claim check before decoding megabytes.
    refuse_paused(
        load_own_claim(
            project_id=call.project.pk,
            pivot_id=request.pivot_id,
            claim_token=request.claim_token,
            lock=False,
        )
    )
    data = _decode(request.image_base64)
    staged = stage_shot(
        call,
        pivot_id=request.pivot_id,
        claim_token=request.claim_token,
        mime=ManualShotMime(request.mime),
        data=data,
        capture_source=ScreenshotCaptureSource.NONE.value,
        hint=SMALLER_HINT,
    )
    return AddScreenshotResponse(
        shot_id=staged.shot_id,
        size=staged.size,
        width=staged.width,
        height=staged.height,
    )
