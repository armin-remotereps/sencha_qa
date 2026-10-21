from __future__ import annotations


class TestRailRunImportError(Exception):
    """Base for run-import failures; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TestRailRunReferenceError(TestRailRunImportError):
    """The pasted text is not a TestRail run URL or run ID."""


class TestRailHostMismatchError(TestRailRunReferenceError):
    """The pasted URL points at a different TestRail instance than the project's."""


class TestRailRunNotFoundError(TestRailRunImportError):
    """TestRail has no run with that ID, or the account cannot see it."""


class TestRailRunImportInProgressError(TestRailRunImportError):
    """The same run is already being imported into this project."""
