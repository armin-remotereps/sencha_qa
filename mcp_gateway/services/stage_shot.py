"""Staging one screenshot of the MCP tester's claimed case.

Shared by ``add_screenshot`` (bytes the client sent) and
``capture_screenshot`` (bytes the test machine captured). ``stage_shot``
runs the manual screenshot check (``load_manual_image``: format matches the
type, pixel cap, full decode) outside any transaction, builds the capture
preview from that same decode when asked, then, under the case
row lock, checks the claim again, enforces the per-claim caps and inserts an
``McpStagedShot`` with a server-minted ``shot_key``. A later ``mark_step``
binds it to a step; ``hand_in`` stores it.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Final

from django.db import transaction
from django.db.models import Count, Sum

from controller_client.protocol import ManualShotMime
from mcp_gateway.exceptions import DecodeSlotTimeout, McpToolError
from mcp_gateway.image_preview import make_preview
from mcp_gateway.limits import MCP_STAGED_BYTES_MAX, MCP_STAGED_SHOTS_MAX
from mcp_gateway.models import McpStagedShot
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.own_claim import load_own_claim, refuse_paused
from projects.manual_screenshot_services import load_manual_image

# 12 random bytes: 16 URL-safe characters (the protocol allows 24).
_SHOT_KEY_BYTES: Final = 12
# Appended to an image refusal of a client-sent screenshot.
SMALLER_HINT: Final = (
    " Capture only the application window or a region of it, or send a JPEG."
)
_TOO_MANY: Final = (
    f"This case already has {MCP_STAGED_SHOTS_MAX} screenshots. Mark the steps "
    "with the ones you have and hand the case in."
)
_TOO_MANY_BYTES: Final = (
    "This case's screenshots already use the storage allowed for one case."
)
# Full decodes running at once in this process: each holds the decoded
# bitmap (up to the pixel cap) in memory, so concurrent calls queue here.
# The capture preview is built from the same decode, inside the slot.
DECODE_SLOTS: Final = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class CheckedImage:
    width: int
    height: int
    # The capture preview (JPEG), only when asked for.
    preview_jpeg: bytes | None


@dataclass(frozen=True)
class StagedShot:
    shot_id: str
    size: int
    width: int
    height: int
    preview_jpeg: bytes | None


def check_image(
    mime: ManualShotMime,
    data: bytes,
    *,
    hint: str,
    preview: bool = False,
    deadline: float | None = None,
) -> CheckedImage:
    """Refuse an unstorable image (``hint`` appended); its size, and the
    preview when ``preview``, from one decode under a decode slot.

    With ``deadline`` (a ``time.monotonic()`` value) the slot is waited for
    until then only: ``DecodeSlotTimeout`` after it. Without, it waits.
    """
    if deadline is None:
        DECODE_SLOTS.acquire()
    elif not DECODE_SLOTS.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise DecodeSlotTimeout()
    try:
        reason, image = load_manual_image(mime, data)
        if image is None:
            raise McpToolError(reason + hint)
        with image:
            width, height = image.size
            jpeg = make_preview(image) if preview else None
    finally:
        DECODE_SLOTS.release()
    return CheckedImage(width=int(width), height=int(height), preview_jpeg=jpeg)


def refuse_full(*, pivot_id: int, claim_token: str, adding: int, hint: str) -> None:
    """Refuse when the claim's staged shots cannot take ``adding`` more bytes
    (one query; run it under the case row lock to enforce the caps)."""
    staged = McpStagedShot.objects.filter(
        pivot_id=pivot_id, claim_token=claim_token
    ).aggregate(count=Count("id"), size=Sum("size"))
    if staged["count"] >= MCP_STAGED_SHOTS_MAX:
        raise McpToolError(_TOO_MANY)
    if (staged["size"] or 0) + adding > MCP_STAGED_BYTES_MAX:
        raise McpToolError(_TOO_MANY_BYTES + hint)


def stage_shot(
    call: McpCall,
    *,
    pivot_id: int,
    claim_token: str,
    mime: ManualShotMime,
    data: bytes,
    capture_source: str,
    hint: str,
    preview: bool = False,
    deadline: float | None = None,
) -> StagedShot:
    """Check and stage ``data`` for the claim; raises ``McpToolError``
    (``DecodeSlotTimeout`` past ``deadline``, before anything is staged).

    ``capture_source`` is a ``projects.models.ScreenshotCaptureSource``
    value, copied to the stored screenshot at hand-in. ``hint`` is appended
    to the size and image refusals. ``preview`` and ``deadline`` as in
    ``check_image``.
    """
    checked = check_image(mime, data, hint=hint, preview=preview, deadline=deadline)
    shot_key = secrets.token_urlsafe(_SHOT_KEY_BYTES)
    if deadline is not None and time.monotonic() >= deadline:
        # The client gave up by now: never stage a shot it cannot see.
        raise DecodeSlotTimeout()
    with transaction.atomic():
        # The case row lock serializes the caps with other staging calls; a
        # claim that ended meanwhile drops the image here.
        refuse_paused(
            load_own_claim(
                project_id=call.project.pk,
                pivot_id=pivot_id,
                claim_token=claim_token,
                lock=True,
            )
        )
        refuse_full(
            pivot_id=pivot_id, claim_token=claim_token, adding=len(data), hint=hint
        )
        McpStagedShot.objects.create(
            pivot_id=pivot_id,
            claim_token=claim_token,
            shot_key=shot_key,
            mime=mime.value,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
            capture_source=capture_source,
        )
    return StagedShot(
        shot_id=shot_key,
        size=len(data),
        width=checked.width,
        height=checked.height,
        preview_jpeg=checked.preview_jpeg,
    )
