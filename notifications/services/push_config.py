"""Whether Web Push is configured on this server (VAPID keys and subject)."""

from __future__ import annotations

from django.conf import settings

from notifications.exceptions import PushNotConfiguredError


def push_configured() -> bool:
    """Push is enabled only when the VAPID key pair and the subject are all set."""
    return bool(
        settings.WEBPUSH_VAPID_PUBLIC_KEY
        and settings.WEBPUSH_VAPID_PRIVATE_KEY
        and settings.WEBPUSH_VAPID_SUBJECT
    )


def ensure_push_configured() -> None:
    if not push_configured():
        raise PushNotConfiguredError()


def vapid_public_key() -> str:
    """The application server key browsers subscribe with ("" when disabled)."""
    return str(settings.WEBPUSH_VAPID_PUBLIC_KEY) if push_configured() else ""
