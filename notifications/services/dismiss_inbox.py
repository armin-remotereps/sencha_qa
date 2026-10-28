"""Dismiss items from the user's own attention inbox.

Every update is scoped to the user, so an id of someone else's row behaves as
if it did not exist. After the change commits the user's other tabs are
nudged to recount.
"""

from __future__ import annotations

from datetime import datetime
from functools import partial

from django.db import models, transaction
from django.utils import timezone

from accounts.models import CustomUser
from notifications.exceptions import InboxItemNotFoundError
from notifications.models import MissedFailure, MissedReply
from notifications.services.inbox_broadcast import broadcast_inbox_changed


class InboxKind(models.TextChoices):
    """Which list "Dismiss all" clears."""

    FAILURES = "failures", "Missed failures"
    REPLIES = "replies", "Missed replies"


def _after_commit(user: CustomUser) -> None:
    transaction.on_commit(partial(broadcast_inbox_changed, [user.pk]))


def _dismiss_one(
    queryset: models.QuerySet[MissedFailure] | models.QuerySet[MissedReply],
    user: CustomUser,
    item_id: int,
) -> None:
    with transaction.atomic():
        updated = queryset.filter(
            pk=item_id, user=user, dismissed_at__isnull=True
        ).update(dismissed_at=timezone.now())
        if updated == 0:
            raise InboxItemNotFoundError()
        _after_commit(user)


def dismiss_missed_failure(user: CustomUser, item_id: int) -> None:
    """Dismiss one of the user's open missed failures.

    Raises ``InboxItemNotFoundError`` when it is not the user's or already gone.
    """
    _dismiss_one(MissedFailure.objects.all(), user, item_id)


def dismiss_missed_reply(user: CustomUser, item_id: int) -> None:
    """Dismiss one of the user's open missed replies.

    Raises ``InboxItemNotFoundError`` when it is not the user's or already gone.
    """
    _dismiss_one(MissedReply.objects.all(), user, item_id)


def dismiss_all(user: CustomUser, kind: InboxKind, *, up_to: datetime) -> int:
    """Dismiss the user's open items of ``kind`` that occurred at or before ``up_to``.

    ``up_to`` is when the page was rendered, so an item that arrived after the
    user looked at the list stays. Returns how many rows were dismissed.
    """
    now = timezone.now()
    up_to = min(up_to, now)
    model: type[MissedFailure] | type[MissedReply] = (
        MissedFailure if kind == InboxKind.FAILURES else MissedReply
    )
    with transaction.atomic():
        dismissed = model.objects.filter(
            user=user, dismissed_at__isnull=True, occurred_at__lte=up_to
        ).update(dismissed_at=now)
        if dismissed:
            _after_commit(user)
    return dismissed
