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


class BrowserUnavailableError(InfrastructureError):
    """The browser a case asked for cannot be driven on the test machine.

    ``safe_message`` is shown in the UI and pushed to TestRail; ``detail`` holds
    the runner's own text (local paths, OS user name) for the server log only.
    """

    def __init__(self, safe_message: str, *, detail: str = "") -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.detail = detail


class RunnerOutdatedError(InfrastructureError):
    """The connected Test Runner lacks an optional capability the case needs.

    The message is fixed server text, shown in the UI and pushed to TestRail.
    """


class VisualEngineError(ControllerActionError, InfrastructureError):
    """The visual engine (OmniParser) failed or is not ready."""


class RunCaseCheckError(Exception):
    """No selected case could be checked or unchecked; ``message`` is user-safe."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CheckedPushError(Exception):
    """Base for "Send checked to TestRail" refusals; ``message`` is user-safe."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunNotFinishedError(CheckedPushError):
    """The run is still waiting or running."""


class NothingToSendError(CheckedPushError):
    """No checked, unsent case with a source TestRail run."""


class CheckedPushInProgressError(CheckedPushError):
    """A send for this run is already in progress."""


class RunResumeError(Exception):
    """A run or case cannot be resumed; ``message`` is user-safe."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunRerunError(Exception):
    """Cases cannot be re-run in place; ``message`` is user-safe."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunHelpError(Exception):
    """A Need help action cannot be taken; ``message`` is user-safe."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ApplicationContextError(Exception):
    """Base for application context writes; ``message`` is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ApplicationContextConflictError(ApplicationContextError):
    """The context changed since the writer read it (revision moved)."""

    def __init__(self, expected_revision: int, current_revision: int) -> None:
        super().__init__(
            f"The application context changed since you read it (revision "
            f"{expected_revision} → {current_revision})."
        )
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class ApplicationContextTooLongError(ApplicationContextError):
    """The notes exceed ``PROJECT_PROMPT_MAX_LENGTH``."""

    def __init__(self, length: int, max_length: int) -> None:
        super().__init__(
            f"The application context is {length} characters; the limit is "
            f"{max_length}."
        )


class ApplicationContextVersionNotFoundError(ApplicationContextError):
    """No such version in this project's history."""

    def __init__(self) -> None:
        super().__init__("That application context version no longer exists.")


class PlanRevisionError(Exception):
    """A plan redesign cannot be proposed or discarded; ``message`` is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ResultChangeError(Exception):
    """A result change cannot be proposed, applied or discarded; ``message`` is safe."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProjectMembershipError(Exception):
    """Base for invite / leave failures; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MemberNotFoundError(ProjectMembershipError):
    """No active account has that email (unknown and inactive look the same)."""

    def __init__(self) -> None:
        super().__init__("No active user with that email.")


class AlreadyMemberError(ProjectMembershipError):
    """The invited user is already a member of the project."""

    def __init__(self) -> None:
        super().__init__("That user is already a member of this project.")


class MemberInviteThrottledError(ProjectMembershipError):
    """The inviter made too many invite attempts in the current window."""

    def __init__(self, window_seconds: int) -> None:
        super().__init__(
            f"Too many invites. Try again in {window_seconds // 60} minutes."
        )
        self.window_seconds = window_seconds


class LastProjectMemberError(ProjectMembershipError):
    """Leaving would leave the project with no other active member."""

    def __init__(self) -> None:
        super().__init__(
            "You are the only active member. Add someone before you leave."
        )


class NotProjectMemberError(ProjectMembershipError):
    """The user is no longer a member (they left in another tab)."""

    def __init__(self) -> None:
        super().__init__("You are not a member of this project.")
