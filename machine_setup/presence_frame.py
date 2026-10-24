"""The setup socket's one client frame: whether a tab shows a conversation."""

from __future__ import annotations

import json
from dataclasses import dataclass

# A presence frame is a few dozen bytes; anything bigger is not one.
MAX_FRAME_BYTES = 256


@dataclass(frozen=True)
class PresenceFrame:
    conversation_id: int
    visible: bool


def parse_presence_frame(text: str | None) -> PresenceFrame | None:
    """``{"type": "presence", "visible": bool, "conversation_id": int}`` or None."""
    # Characters never outnumber UTF-8 bytes: the cheap check runs first.
    if text is None or len(text) > MAX_FRAME_BYTES:
        return None
    if len(text.encode()) > MAX_FRAME_BYTES:
        return None
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("type") != "presence":
        return None
    visible = frame.get("visible")
    conversation_id = frame.get("conversation_id")
    if not isinstance(visible, bool):
        return None
    if not isinstance(conversation_id, int) or isinstance(conversation_id, bool):
        return None
    return PresenceFrame(conversation_id=conversation_id, visible=visible)
