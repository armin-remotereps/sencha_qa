"""Delete a test session and its case rows."""

from __future__ import annotations

from accounts.models import CustomUser
from test_sessions.exceptions import SessionNotFoundError
from test_sessions.models import TestSession


def delete_session(*, user: CustomUser, session_id: int) -> None:
    """Delete ``user``'s session ``session_id``; its cases cascade (the
    collector fast-deletes them: 2 to 3 queries, in a transaction).
    ``SessionNotFoundError`` when it is not the user's."""
    deleted, _ = TestSession.objects.filter(id=session_id, user=user).delete()
    if not deleted:
        raise SessionNotFoundError()
