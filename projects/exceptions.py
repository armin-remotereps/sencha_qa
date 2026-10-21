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


class ControllerNotConnectedError(Exception):
    """No Test Runner is connected and no active run is waiting for one."""


class ControllerReconnectTimeoutError(Exception):
    """The Test Runner did not reconnect within the grace period of an active run."""

    def __init__(self, grace_seconds: int) -> None:
        super().__init__(
            f"Test Runner did not reconnect within {grace_seconds}s; run aborted"
        )
        self.grace_seconds = grace_seconds
