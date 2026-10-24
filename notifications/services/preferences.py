"""The user's own notification preference: turn on/off, kinds, topic.

Every function acts on the given user's own row only. The topic is a secret:
it is generated here, returned only through ``get_preference_view`` for its
owner, and never logged.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import CustomUser
from notifications.exceptions import NotificationsOffError
from notifications.models import NotificationPreference
from notifications.services.ntfy_config import (
    ensure_ntfy_configured,
    ntfy_subscribe_links,
)
from notifications.services.preference_view import PreferenceView

# 24 random bytes -> 32 URL-safe characters (about 192 bits), ntfy's alphabet.
TOPIC_RANDOM_BYTES = 24
# A unique-topic collision is astronomically unlikely; one retry is plenty.
_TOPIC_ATTEMPTS = 2


def _new_topic() -> str:
    # ntfy_configured() has validated the prefix (at most 16 characters).
    return str(settings.NTFY_TOPIC_PREFIX) + secrets.token_urlsafe(TOPIC_RANDOM_BYTES)


def get_preference_view(user: CustomUser) -> PreferenceView | None:
    """The user's own preference with its subscribe links, or None if never on.

    Call it only for the request's own user: the result holds the topic.
    """
    row = (
        NotificationPreference.objects.filter(user=user)
        .values_list(
            "enabled", "topic", "notify_need_help", "notify_failed", "notify_chat_reply"
        )
        .first()
    )
    if row is None:
        return None
    enabled, topic, notify_need_help, notify_failed, notify_chat_reply = row
    return PreferenceView(
        enabled=enabled,
        topic=topic,
        notify_need_help=notify_need_help,
        notify_failed=notify_failed,
        notify_chat_reply=notify_chat_reply,
        links=ntfy_subscribe_links(topic),
    )


def enable(user: CustomUser) -> None:
    """Turn the user's notifications on, creating their topic the first time.

    An existing row keeps its topic and kind choices, so a device already
    subscribed keeps working. Raises ``NotificationsNotConfiguredError``.
    """
    ensure_ntfy_configured()
    for attempt in range(_TOPIC_ATTEMPTS):
        try:
            with transaction.atomic():
                _, created = NotificationPreference.objects.get_or_create(
                    user=user, defaults={"topic": _new_topic(), "enabled": True}
                )
                if not created:
                    NotificationPreference.objects.filter(
                        user=user, enabled=False
                    ).update(enabled=True, updated_at=timezone.now())
            return
        except IntegrityError:
            # Another user already holds the generated topic; draw again.
            if attempt == _TOPIC_ATTEMPTS - 1:
                raise


def disable(user: CustomUser) -> None:
    """Turn the user's notifications off; the row and topic are kept."""
    NotificationPreference.objects.filter(user=user, enabled=True).update(
        enabled=False, updated_at=timezone.now()
    )


def regenerate_topic(user: CustomUser) -> None:
    """Give the user a new random topic; the old one receives nothing more.

    Raises ``NotificationsNotConfiguredError``, or ``NotificationsOffError``
    when the user never turned notifications on.
    """
    ensure_ntfy_configured()
    for attempt in range(_TOPIC_ATTEMPTS):
        try:
            with transaction.atomic():
                updated = NotificationPreference.objects.filter(user=user).update(
                    topic=_new_topic(), updated_at=timezone.now()
                )
        except IntegrityError:
            if attempt == _TOPIC_ATTEMPTS - 1:
                raise
            continue
        if updated == 0:
            raise NotificationsOffError()
        return


def update_kinds(
    user: CustomUser, *, need_help: bool, failed: bool, chat_reply: bool
) -> None:
    """Choose which kinds of notification the user gets.

    Raises ``NotificationsOffError`` when the user never turned notifications on.
    """
    updated = NotificationPreference.objects.filter(user=user).update(
        notify_need_help=need_help,
        notify_failed=failed,
        notify_chat_reply=chat_reply,
        updated_at=timezone.now(),
    )
    if updated == 0:
        raise NotificationsOffError()
