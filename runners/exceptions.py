"""Domain errors of the Test Runner domain; ``message`` is safe to show the user."""

from __future__ import annotations


class RunnerError(Exception):
    """Base for Test Runner failures."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunnerNotFoundError(RunnerError):
    """No usable runner with that id (missing, archived, or not the caller's)."""

    def __init__(self) -> None:
        super().__init__("Test Runner not found.")


class RunnerPermissionError(RunnerError):
    """The user may not manage or assign this runner."""

    def __init__(self, message: str = "Only the runner's owner can do that.") -> None:
        super().__init__(message)


class RunnerNameInvalidError(RunnerError):
    """The name is blank, too long or holds control characters."""


class RunnerNameTakenError(RunnerError):
    """The owner already has a runner with this name (case-insensitive)."""

    def __init__(self) -> None:
        super().__init__("You already have a Test Runner with this name.")


class RunnerLimitReachedError(RunnerError):
    """The owner already has ``RUNNER_MAX_PER_USER`` non-archived runners."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"You can have at most {limit} Test Runners.")
        self.limit = limit


class RunnerHeldError(RunnerError):
    """The runner serves another holder (run, Manual case, setup, scan, update).

    ``message`` comes from ``projects.runner_use.RunnerHold.message``.
    """


class RunnerAssignedError(RunnerError):
    """The runner is assigned to an active run (archive refused)."""

    def __init__(self) -> None:
        super().__init__(
            "This Test Runner is assigned to an active run. Release it or "
            "assign the run to another runner first."
        )


class RunnerNotReadyError(RunnerError):
    """The runner is not connected, or its phone or tablet is not ready."""


class RunAssignmentError(RunnerError):
    """The run's state does not allow (re)assigning a runner (Q6)."""
