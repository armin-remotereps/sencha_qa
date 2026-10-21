from __future__ import annotations

from agents.exceptions import InfrastructureError


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


class ControllerReconnectTimeoutError(InfrastructureError):
    """The Test Runner did not reconnect within the grace period of an active run."""

    def __init__(self, grace_seconds: int) -> None:
        super().__init__(
            f"Test Runner did not reconnect within {grace_seconds}s; run aborted"
        )
        self.grace_seconds = grace_seconds


class ControllerActionError(Exception):
    pass


class TestRunnerUnavailableError(ControllerActionError, InfrastructureError):
    """The Test Runner could not carry out an action; the app was not judged."""


class TestRunnerDisconnectedError(TestRunnerUnavailableError):
    """The connection dropped again while an interrupted action was re-sent."""


class TestRunnerUnresponsiveError(TestRunnerUnavailableError):
    """A runner-local action got no reply while the runner still held the connection."""


class VisualEngineError(ControllerActionError, InfrastructureError):
    """The visual engine (OmniParser) failed or is not ready."""
