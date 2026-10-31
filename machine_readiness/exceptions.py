"""Domain errors of machine readiness; every ``message`` is safe to show."""

from __future__ import annotations

from collections.abc import Sequence


class MachineReadinessError(Exception):
    """Base class; ``message`` is safe to show to a member or an agent."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ArtifactProfileConflictError(MachineReadinessError):
    """The profile changed since ``expected_revision`` was read."""

    def __init__(self, message: str, *, current_revision: int) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class ArtifactProfileInvalidError(MachineReadinessError):
    """A profile write broke a rule; ``errors`` lists every problem found."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors) or "The artifacts profile is invalid.")
        self.errors: tuple[str, ...] = tuple(errors)


class ArtifactKnowledgeRefError(ArtifactProfileInvalidError):
    """Items cite knowledge base ids that are not items of the project.

    ``item_ids`` are the refused ids, so a form can mark the fields citing them.
    """

    def __init__(self, errors: Sequence[str], *, item_ids: Sequence[int]) -> None:
        super().__init__(errors)
        self.item_ids: tuple[int, ...] = tuple(item_ids)


class ArtifactProfileVersionNotFoundError(MachineReadinessError):
    """No such profile version in this project."""


class ArtifactScanUnavailableError(MachineReadinessError):
    """No scan possible: runner offline or too old, or no base directory."""


class QuickCheckRefusedError(MachineReadinessError):
    """A run is Started, a setup turn is live or the runner is updating."""


class Stage0AnalysisBusyError(MachineReadinessError):
    """Another precondition analysis of the project is running."""


class RequirementNotFoundError(MachineReadinessError):
    """No such stage-0 requirement in this project."""
