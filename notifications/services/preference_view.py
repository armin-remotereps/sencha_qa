"""The owner-only view of a user's notification preference."""

from __future__ import annotations

from dataclasses import dataclass

from notifications.services.ntfy_config import NtfySubscribeLinks


@dataclass(frozen=True)
class PreferenceView:
    """What the Notifications page shows its owner.

    ``topic`` and ``links`` are secrets: build this only for the request's own
    user, never for anyone else.
    """

    enabled: bool
    topic: str
    notify_need_help: bool
    notify_failed: bool
    links: NtfySubscribeLinks
