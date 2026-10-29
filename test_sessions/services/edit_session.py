"""Rename a test session or change its note."""

from __future__ import annotations

from django.utils import timezone

from accounts.models import CustomUser
from test_sessions.exceptions import SessionNotFoundError
from test_sessions.models import TestSession


def edit_session(
    *, user: CustomUser, session_id: int, name: str, note: str
) -> TestSession:
    """Set the name and note of ``user``'s session ``session_id`` (2 queries);
    ``SessionNotFoundError`` when it is not the user's. Times never change."""
    session = TestSession.objects.filter(id=session_id, user=user).first()
    if session is None:
        raise SessionNotFoundError()
    session.name = name.strip()
    session.note = note.strip()
    session.updated_at = timezone.now()
    session.save(update_fields=["name", "note", "updated_at"])
    return session
