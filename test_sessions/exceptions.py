from __future__ import annotations


class TestSessionError(Exception):
    """Base for test session failures; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SessionNotFoundError(TestSessionError):
    """No session with that id belongs to the user."""

    def __init__(self) -> None:
        super().__init__("Test session not found.")


class SessionAlreadyOpenError(TestSessionError):
    """The user already has an open session (one per user)."""

    def __init__(self, open_session_id: int | None) -> None:
        super().__init__("You already have an open test session.")
        self.open_session_id = open_session_id


class SessionAlreadyClosedError(TestSessionError):
    """Stop was pressed on a session that has already ended."""

    def __init__(self) -> None:
        super().__init__("This test session has already ended.")


class SessionExportThrottledError(TestSessionError):
    """Too many CSV exports in a short window."""

    def __init__(self) -> None:
        super().__init__("Too many exports. Wait a minute and try again.")
