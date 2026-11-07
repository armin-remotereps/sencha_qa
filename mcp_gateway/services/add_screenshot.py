"""Use case: the MCP tester sends one screenshot of its claimed case.

The client captured the image itself (the window, screen or region it
chose); the server never captures, crops, picks a window or downscales. It
checks the claim, the base64 length before decoding, decodes strictly, runs
the manual screenshot check (``check_manual_image``: format matches the
type, pixel cap, full decode) and stages the bytes in ``McpStagedShot``
within the per-claim caps. A later ``mark_step`` binds it to a step.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import secrets
import threading
from typing import Final

from django.db import transaction
from django.db.models import Count, Sum
from PIL import Image

from controller_client.protocol import ManualShotMime
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.limits import (
    MCP_SHOT_MAX_BASE64_CHARS,
    MCP_STAGED_BYTES_MAX,
    MCP_STAGED_SHOTS_MAX,
)
from mcp_gateway.models import McpStagedShot
from mcp_gateway.schemas import AddScreenshotRequest, AddScreenshotResponse
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.own_claim import load_own_claim, refuse_paused
from projects.manual_screenshot_services import check_manual_image

# 12 random bytes: 16 URL-safe characters (the protocol allows 24).
_SHOT_KEY_BYTES: Final = 12
_SMALLER: Final = (
    " Capture only the application window or a region of it, or send a JPEG."
)
_TOO_LARGE: Final = "The image is larger than the server accepts." + _SMALLER
_NOT_BASE64: Final = (
    "image_base64 is not valid base64 (standard alphabet, padded, no data: "
    "prefix, no line breaks)."
)
_EMPTY: Final = "The image is empty."
_TOO_MANY: Final = (
    f"This case already has {MCP_STAGED_SHOTS_MAX} screenshots. Mark the steps "
    "with the ones you have and hand the case in."
)
_TOO_MANY_BYTES: Final = (
    "This case's screenshots already use the storage allowed for one case." + _SMALLER
)
# Full decodes running at once in this process: each holds the decoded
# bitmap (up to the pixel cap) in memory, so concurrent calls queue here.
_DECODE_SLOTS: Final = threading.BoundedSemaphore(2)


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


def _check_image(mime: ManualShotMime, data: bytes) -> tuple[int, int]:
    """Refuse an unstorable image; its width and height."""
    with _DECODE_SLOTS:
        reason = check_manual_image(mime, data)
    if reason:
        raise McpToolError(reason + _SMALLER)
    # Header only: the full decode above already passed.
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    return int(width), int(height)


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
    mime = ManualShotMime(request.mime)
    data = _decode(request.image_base64)
    width, height = _check_image(mime, data)
    shot_key = secrets.token_urlsafe(_SHOT_KEY_BYTES)
    with transaction.atomic():
        # The case row lock serializes the caps with other staging calls.
        refuse_paused(
            load_own_claim(
                project_id=call.project.pk,
                pivot_id=request.pivot_id,
                claim_token=request.claim_token,
                lock=True,
            )
        )
        staged = McpStagedShot.objects.filter(
            pivot_id=request.pivot_id, claim_token=request.claim_token
        ).aggregate(count=Count("id"), size=Sum("size"))
        if staged["count"] >= MCP_STAGED_SHOTS_MAX:
            raise McpToolError(_TOO_MANY)
        if (staged["size"] or 0) + len(data) > MCP_STAGED_BYTES_MAX:
            raise McpToolError(_TOO_MANY_BYTES)
        McpStagedShot.objects.create(
            pivot_id=request.pivot_id,
            claim_token=request.claim_token,
            shot_key=shot_key,
            mime=mime.value,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )
    return AddScreenshotResponse(
        shot_id=shot_key, size=len(data), width=width, height=height
    )
