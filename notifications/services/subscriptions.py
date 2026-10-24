"""Save, remove and list a user's Web Push devices.

Endpoints and keys are device secrets: nothing here returns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db import transaction

from accounts.models import CustomUser
from notifications.models import PushSubscription
from notifications.services.endpoint_policy import ensure_endpoint_allowed
from notifications.services.push_config import ensure_push_configured

USER_AGENT_MAX_LENGTH = 255
# Devices kept per user; saving one more evicts that user's oldest.
MAX_SUBSCRIPTIONS_PER_USER = 20


@dataclass(frozen=True)
class PushDevice:
    """One device as the notifications page shows it (no endpoint, no keys)."""

    id: int
    label: str
    created_at: datetime
    last_success_at: datetime | None


def save_subscription(
    *,
    user: CustomUser,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str,
) -> None:
    """Store (or refresh) a browser's subscription for ``user``.

    An endpoint belongs to one browser profile; when another account signed in
    there subscribes, the device moves to that account. A user keeps at most
    ``MAX_SUBSCRIPTIONS_PER_USER`` devices: beyond that, their oldest other
    devices are removed in the same transaction.
    """
    ensure_push_configured()
    ensure_endpoint_allowed(endpoint)
    with transaction.atomic():
        saved, _ = PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                "user": user,
                "p256dh": p256dh,
                "auth": auth,
                "user_agent": user_agent[:USER_AGENT_MAX_LENGTH],
            },
        )
        evicted = list(
            PushSubscription.objects.filter(user=user)
            .exclude(pk=saved.pk)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)[MAX_SUBSCRIPTIONS_PER_USER - 1 :]
        )
        if evicted:
            PushSubscription.objects.filter(pk__in=evicted).delete()


def delete_subscription_by_endpoint(*, user: CustomUser, endpoint: str) -> bool:
    """Remove the user's own subscription with this endpoint; True if one went."""
    deleted, _ = PushSubscription.objects.filter(user=user, endpoint=endpoint).delete()
    return deleted > 0


def delete_subscription_by_id(*, user: CustomUser, subscription_id: int) -> bool:
    """Remove one of the user's own devices; True if one went."""
    deleted, _ = PushSubscription.objects.filter(user=user, pk=subscription_id).delete()
    return deleted > 0


def count_user_devices(user: CustomUser) -> int:
    return PushSubscription.objects.filter(user=user).count()


def list_user_devices(user: CustomUser) -> list[PushDevice]:
    rows = PushSubscription.objects.filter(user=user).values_list(
        "id", "user_agent", "created_at", "last_success_at"
    )
    return [
        PushDevice(
            id=row_id,
            label=device_label(user_agent),
            created_at=created_at,
            last_success_at=last_success_at,
        )
        for row_id, user_agent, created_at, last_success_at in rows
    ]


def _browser_name(user_agent: str) -> str:
    # Order matters: Edge and Opera also claim Chrome, Chrome also claims Safari.
    for marker, name in (
        ("Edg/", "Edge"),
        ("OPR/", "Opera"),
        ("Firefox/", "Firefox"),
        ("FxiOS/", "Firefox"),
        ("CriOS/", "Chrome"),
        ("Chrome/", "Chrome"),
        ("Safari/", "Safari"),
    ):
        if marker in user_agent:
            return name
    return "Browser"


def _os_name(user_agent: str) -> str:
    for marker, name in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Windows", "Windows"),
        ("Mac OS X", "macOS"),
        ("CrOS", "ChromeOS"),
        ("Linux", "Linux"),
    ):
        if marker in user_agent:
            return name
    return ""


def device_label(user_agent: str) -> str:
    """A short "Chrome on macOS" style name for a stored User-Agent."""
    if not user_agent:
        return "Unknown device"
    os_name = _os_name(user_agent)
    browser = _browser_name(user_agent)
    return f"{browser} on {os_name}" if os_name else browser
