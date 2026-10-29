"""Start a test session (at most one open session per user)."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import CustomUser
from test_sessions.exceptions import SessionAlreadyOpenError
from test_sessions.models import TestSession
from test_sessions.services.session_queries import open_session_id


def start_session(*, user: CustomUser, name: str, note: str) -> TestSession:
    """Open a new session for ``user`` now (1 query).

    The partial unique constraint ``test_session_one_open_per_user`` decides
    races: a second open session raises ``SessionAlreadyOpenError`` carrying
    the open one's id (1 more query).
    """
    try:
        with transaction.atomic():
            return TestSession.objects.create(
                user=user,
                name=name.strip(),
                note=note.strip(),
                started_at=timezone.now(),
            )
    except IntegrityError as error:
        existing_id = open_session_id(user)
        if existing_id is None:
            # Not the one-open-session constraint: a real error.
            raise
        raise SessionAlreadyOpenError(existing_id) from error
