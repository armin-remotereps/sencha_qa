"""Put a setup turn that ended while its sender was away into their inbox."""

from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction

from notifications.models import (
    MISSED_REPLY_PREVIEW_MAX_LENGTH,
    MissedReply,
    MissedReplyOutcome,
)

logger = logging.getLogger(__name__)


def record_missed_reply(
    *,
    user_id: int,
    conversation_id: int,
    outcome: MissedReplyOutcome,
    preview: str,
    occurred_at: datetime,
) -> None:
    """Add (or re-open) the user's missed reply for the conversation.

    One upsert: an existing row for (user, conversation) takes the new
    outcome, preview and time and is no longer dismissed. ``preview`` is plain
    text and is cut to ``MISSED_REPLY_PREVIEW_MAX_LENGTH``. The caller sends
    the live broadcast on commit. Runs in a savepoint and never raises: logs
    ids only.
    """
    try:
        with transaction.atomic():
            MissedReply.objects.bulk_create(
                [
                    MissedReply(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        outcome=outcome,
                        preview=preview[:MISSED_REPLY_PREVIEW_MAX_LENGTH],
                        occurred_at=occurred_at,
                        dismissed_at=None,
                    )
                ],
                update_conflicts=True,
                unique_fields=["user", "conversation"],
                update_fields=["outcome", "preview", "occurred_at", "dismissed_at"],
            )
    except Exception:
        logger.exception(
            "Could not record the missed reply of conversation %s for user %s",
            conversation_id,
            user_id,
        )
