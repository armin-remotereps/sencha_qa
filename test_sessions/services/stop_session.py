"""Stop the user's open test session."""

from __future__ import annotations

from django.db import transaction
from django.db.models import DateTimeField, F, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from accounts.models import CustomUser
from test_sessions.exceptions import SessionAlreadyClosedError, SessionNotFoundError
from test_sessions.models import TestSession
from test_sessions.services.record_case_activity import (
    freeze_finished_cases_of_sessions,
)


def stop_session(*, user: CustomUser, session_id: int, note: str | None) -> TestSession:
    """End ``user``'s open session ``session_id`` now and freeze its finished
    cases, in one transaction: a conditional UPDATE (owned and open), the
    freeze (1 query) and a read back, 3 queries.

    ``note`` None keeps the current note. ``SessionNotFoundError`` when the
    session is not the user's; ``SessionAlreadyClosedError`` when it has ended.
    """
    now = timezone.now()
    changes: dict[str, object] = {
        # Never before the start (ends-after-start check), even with clock skew.
        "ended_at": Greatest(Value(now, output_field=DateTimeField()), F("started_at")),
        "updated_at": now,
    }
    if note is not None:
        changes["note"] = note.strip()
    with transaction.atomic():
        stopped = TestSession.objects.filter(
            id=session_id, user=user, ended_at__isnull=True
        ).update(**changes)
        if not stopped:
            if TestSession.objects.filter(id=session_id, user=user).exists():
                raise SessionAlreadyClosedError()
            raise SessionNotFoundError()
        freeze_finished_cases_of_sessions([session_id], now)
        return TestSession.objects.get(id=session_id)
