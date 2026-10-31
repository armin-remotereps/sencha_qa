from __future__ import annotations

from agents.exceptions import InfrastructureError


class MachineSetupError(Exception):
    """A machine setup request that cannot be carried out; ``message`` is user-safe."""

    default_message = "The machine setup request could not be carried out."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class SetupTurnActiveError(MachineSetupError):
    default_message = (
        "The setup agent is still working. Wait for it to finish or stop it first."
    )


class SetupRunActiveError(MachineSetupError):
    default_message = (
        "A test run is in progress on this machine. Wait for it to finish first."
    )


class SetupMachineNotReadyError(MachineSetupError):
    default_message = (
        "The test machine is not ready: the Test Runner must be connected and "
        "the visual engine ready."
    )


class SetupNoActiveTurnError(MachineSetupError):
    default_message = "The setup agent is not working right now."


class SetupRunnerLostError(InfrastructureError):
    """The Test Runner disconnected mid-turn; ends the turn (internal)."""


class HelpChatSendRefusedError(MachineSetupError):
    """The thread exists, but its first message could not be sent.

    ``conversation_id`` is the thread to show the person, with ``message``.
    """

    def __init__(self, message: str, *, conversation_id: int) -> None:
        super().__init__(message)
        self.conversation_id = conversation_id


class ResultChatSendRefusedError(MachineSetupError):
    """The result chat exists, but the message could not be sent.

    ``conversation_id`` is the thread to show the person, with ``message``.
    """

    def __init__(self, message: str, *, conversation_id: int) -> None:
        super().__init__(message)
        self.conversation_id = conversation_id


class SetupDocumentInvalidError(MachineSetupError):
    default_message = "The document could not be attached."


class SetupDocumentNotAllowedError(MachineSetupError):
    default_message = "Documents can be attached in the main chat only."


class SetupKnowledgeRefError(MachineSetupError):
    """A referenced knowledge base item is not in the project (or was deleted)."""

    default_message = (
        "A picked knowledge base item no longer exists. Pick the items again."
    )


class SetupDocumentReadError(MachineSetupError):
    """Tool-facing: unknown document id or page out of range."""

    default_message = "The document could not be read."


class RunProposalInvalidError(MachineSetupError):
    """Tool-facing: the proposed runs break a rule; ``message`` says which."""

    default_message = "The proposed test runs are not valid."


class RunProposalClosedError(MachineSetupError):
    default_message = "This proposal is no longer open."


class RunProposalEmptyError(MachineSetupError):
    default_message = (
        "None of the proposed runs has any case left, so nothing was created."
    )
