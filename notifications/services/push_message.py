"""The content of one Web Push notification."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PushMessage:
    title: str
    body: str
    # Same-origin path the notification opens when clicked.
    url: str
    # Notifications with the same tag replace each other on a device.
    tag: str
