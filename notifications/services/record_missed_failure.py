"""Put a run case the agent judged Failed into every writable member's attention inbox."""

from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction

from accounts.models import CustomUser
from notifications.models import MissedFailure
from projects.models import ProjectRole

logger = logging.getLogger(__name__)


def record_missed_failure(
    *, pivot_id: int, project_id: int, occurred_at: datetime
) -> list[int]:
    """Add (or re-open) the case's missed failure for each active writable project member.

    One query for the member ids and one bulk upsert: a member who already has
    a row for the case gets it back with ``occurred_at`` moved on and
    ``dismissed_at`` cleared. Returns the user ids, for the live broadcast
    (the caller sends it on commit). Runs in a savepoint and never raises, so
    a failure here cannot break the caller's transaction: logs ids only and
    returns an empty list.
    """
    try:
        with transaction.atomic():
            user_ids = list(
                CustomUser.objects.filter(
                    project_memberships__project_id=project_id,
                    project_memberships__role=ProjectRole.MEMBER,
                    is_active=True,
                ).values_list("id", flat=True)
            )
            if not user_ids:
                return []
            MissedFailure.objects.bulk_create(
                [
                    MissedFailure(
                        user_id=user_id,
                        test_run_test_case_id=pivot_id,
                        occurred_at=occurred_at,
                        dismissed_at=None,
                    )
                    for user_id in user_ids
                ],
                update_conflicts=True,
                unique_fields=["user", "test_run_test_case"],
                update_fields=["occurred_at", "dismissed_at"],
            )
        return user_ids
    except Exception:
        logger.exception("Could not record the missed failure of pivot %s", pivot_id)
        return []
