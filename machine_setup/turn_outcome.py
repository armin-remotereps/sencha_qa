"""How a setup turn ended: status, reason and the user-safe texts for the page.

Pure: maps the tool loop's stop reason, or the exception that ended it, to an
outcome. Exception details never reach the texts; they go to the worker log.
"""

from __future__ import annotations

from dataclasses import dataclass

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings

from agents.exceptions import AIServiceError
from agents.types import AgentCancelledError, AgentStopReason
from machine_setup.exceptions import SetupRunnerLostError
from machine_setup.models import SetupTurnEndReason, SetupTurnStatus
from projects.exceptions import (
    ControllerNotConnectedError,
    ControllerReconnectTimeoutError,
    TestRunnerUnavailableError,
    VisualEngineError,
)

_CONTINUE = "Reply 'continue' to carry on."


@dataclass(frozen=True)
class TurnOutcome:
    status: SetupTurnStatus
    ended_reason: SetupTurnEndReason
    # Shown on the turn; empty unless the turn failed.
    error_message: str
    # Text of the notice row in the chat; empty for an answered turn.
    notice: str


def classify_turn_end(
    *,
    stop_reason: AgentStopReason | None = None,
    exception: BaseException | None = None,
    iterations: int = 0,
    conversation_archived: bool = False,
) -> TurnOutcome:
    """Outcome of a turn that returned ``stop_reason`` or raised ``exception``."""
    if exception is not None:
        return _from_exception(exception, conversation_archived=conversation_archived)
    if stop_reason is AgentStopReason.TASK_COMPLETE:
        return TurnOutcome(
            SetupTurnStatus.COMPLETED, SetupTurnEndReason.ANSWERED, "", ""
        )
    if stop_reason is AgentStopReason.MAX_ITERATIONS:
        return TurnOutcome(
            SetupTurnStatus.COMPLETED,
            SetupTurnEndReason.ITERATION_LIMIT,
            "",
            f"Stopped after {iterations} steps. {_CONTINUE}",
        )
    if stop_reason is AgentStopReason.TIMEOUT:
        return TurnOutcome(
            SetupTurnStatus.COMPLETED,
            SetupTurnEndReason.TIME_LIMIT,
            "",
            f"Stopped after the {_timeout_minutes()}-minute time limit. {_CONTINUE}",
        )
    if stop_reason is AgentStopReason.CANCELLED:
        return stopped_outcome(conversation_archived=conversation_archived)
    return _internal_error()


def stopped_outcome(*, conversation_archived: bool) -> TurnOutcome:
    """A turn ended by Stop, or by Reset when its conversation is archived."""
    if conversation_archived:
        return TurnOutcome(
            SetupTurnStatus.STOPPED,
            SetupTurnEndReason.RESET,
            "",
            "The chat was reset, so this turn was stopped.",
        )
    return TurnOutcome(
        SetupTurnStatus.STOPPED,
        SetupTurnEndReason.STOPPED_BY_USER,
        "",
        "Stopped. Actions already done on the test machine stay done.",
    )


def transcript_invalid_outcome() -> TurnOutcome:
    message = "The chat history can no longer be used. Reset the chat to continue."
    return TurnOutcome(
        SetupTurnStatus.FAILED,
        SetupTurnEndReason.TRANSCRIPT_INVALID,
        message,
        message,
    )


def _from_exception(
    exception: BaseException, *, conversation_archived: bool
) -> TurnOutcome:
    if isinstance(exception, AgentCancelledError):
        return stopped_outcome(conversation_archived=conversation_archived)
    if isinstance(exception, SoftTimeLimitExceeded):
        return _failed(
            SetupTurnEndReason.TIME_LIMIT,
            f"The turn ran past its {_timeout_minutes()}-minute time limit. "
            f"{_CONTINUE}",
        )
    if isinstance(
        exception,
        (
            SetupRunnerLostError,
            ControllerNotConnectedError,
            ControllerReconnectTimeoutError,
            TestRunnerUnavailableError,
        ),
    ):
        return _failed(
            SetupTurnEndReason.RUNNER_DISCONNECTED,
            "The Test Runner disconnected or stopped responding. Reconnect it, "
            "then reply 'continue' to carry on.",
        )
    if isinstance(exception, VisualEngineError):
        return _failed(
            SetupTurnEndReason.VISUAL_ENGINE,
            "The visual engine failed or is not ready. Wait until the test "
            "machine is ready, then reply 'continue' to carry on.",
        )
    if isinstance(exception, AIServiceError):
        return _failed(
            SetupTurnEndReason.AI_SERVICE,
            f"The AI service failed: {exception.message} {_CONTINUE}",
        )
    return _internal_error()


def _internal_error() -> TurnOutcome:
    return _failed(
        SetupTurnEndReason.INTERNAL_ERROR,
        "Something went wrong while the agent was working. Reply 'continue' to "
        "try again, or reset the chat.",
    )


def _failed(reason: SetupTurnEndReason, message: str) -> TurnOutcome:
    return TurnOutcome(SetupTurnStatus.FAILED, reason, message, message)


def _timeout_minutes() -> int:
    return int(settings.MACHINE_SETUP_TURN_TIMEOUT_SECONDS) // 60
