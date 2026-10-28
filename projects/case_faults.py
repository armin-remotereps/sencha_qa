"""Turn an infrastructure exception into a help-request fault and safe texts.

A fault never blocks a case: it pauses the run with a Need help request, and
only a person marks the case Blocked. Pure: no ORM and no import of
``projects.services``, so case execution, the help services and machine setup
can all use it.
"""

from __future__ import annotations

from celery.exceptions import SoftTimeLimitExceeded

from agents.exceptions import AIServiceError, ResumeStateLostError
from agents.types import OrchestratorCheckpoint
from projects.exceptions import (
    BrowserUnavailableError,
    ControllerReconnectTimeoutError,
    RunnerOutdatedError,
    TestRunnerDisconnectedError,
    TestRunnerUnresponsiveError,
    VisualEngineError,
)
from projects.models import BlockedReason, HelpContinueMode

MAX_FAULT_MESSAGE_CHARS = 500

RESUME_STATE_LOST_PREFIX = (
    "The agent could not restore the state from the earlier steps before resuming."
)

# The first matching type wins, so a specific type must come before any of its
# bases (``ResumeStateLostError`` is an ``InfrastructureError``).
_REASON_BY_TYPE: tuple[tuple[type[BaseException], BlockedReason], ...] = (
    (ResumeStateLostError, BlockedReason.RESUME_STATE_LOST),
    (ControllerReconnectTimeoutError, BlockedReason.RUNNER_DISCONNECTED),
    (TestRunnerDisconnectedError, BlockedReason.RUNNER_DISCONNECTED),
    (TestRunnerUnresponsiveError, BlockedReason.RUNNER_UNRESPONSIVE),
    (VisualEngineError, BlockedReason.VISUAL_ENGINE),
    (BrowserUnavailableError, BlockedReason.BROWSER_UNAVAILABLE),
    (RunnerOutdatedError, BlockedReason.RUNNER_OUTDATED),
    (AIServiceError, BlockedReason.AI_SERVICE),
    (SoftTimeLimitExceeded, BlockedReason.TIME_LIMIT),
)

# What a person (or the setup agent) should fix before pressing Continue.
_ASK_BY_REASON: dict[BlockedReason, str] = {
    BlockedReason.RUNNER_DISCONNECTED: (
        "Reconnect the Test Runner on the test machine, then Continue."
    ),
    BlockedReason.RUNNER_UNRESPONSIVE: (
        "Check that the Test Runner on the test machine is running and "
        "responding, then Continue."
    ),
    BlockedReason.VISUAL_ENGINE: (
        "Start or check a vision worker (Advanced diagnostics on the "
        "Environment page), then Continue."
    ),
    BlockedReason.AI_SERVICE: (
        "Check the AI service (key, quota, provider status), then Continue."
    ),
    BlockedReason.TIME_LIMIT: (
        "Check why the case took so long (a stuck dialog or a slow app), "
        "then Continue."
    ),
    BlockedReason.BROWSER_UNAVAILABLE: (
        "Install the browser on the test machine, then Continue."
    ),
    BlockedReason.RUNNER_OUTDATED: (
        "Update the Test Runner on the test machine, then Continue."
    ),
    BlockedReason.RESUME_STATE_LOST: (
        "Bring the application to the state the step needs, then Continue."
    ),
    BlockedReason.INTERNAL_ERROR: (
        "Check the server log for the error, then Continue."
    ),
}


# Faults after which the machine may have restarted, so the earlier steps'
# state has to be restored before the step runs again. Runner outdated is
# fixed by updating the Test Runner, whose handover closes the browsers and
# apps the old runner opened.
_RESTORE_FAULTS = frozenset(
    {
        BlockedReason.RUNNER_DISCONNECTED,
        BlockedReason.RUNNER_UNRESPONSIVE,
        BlockedReason.RUNNER_OUTDATED,
    }
)


def classify_case_fault(exc: BaseException) -> BlockedReason:
    for exc_type, reason in _REASON_BY_TYPE:
        if isinstance(exc, exc_type):
            return reason
    return BlockedReason.INTERNAL_ERROR


def fault_message(exc: BaseException, reason: BlockedReason) -> str:
    """User-facing text: shown in the UI, to the setup agent and in TestRail.

    ``AIServiceError.message`` is built by the LLM client without URLs, headers
    or keys. Visual-engine and unexpected errors show fixed text, never the
    exception's, which may contain local paths or anything else.
    ``BrowserUnavailableError.safe_message`` is built by the server.
    """
    if reason == BlockedReason.TIME_LIMIT:
        text = "The case ran past its time limit before the agent reached a verdict."
    elif isinstance(exc, ResumeStateLostError):
        # The summary is model-written screen text; templates render it escaped.
        summary = exc.summary.strip()
        text = (
            f"{RESUME_STATE_LOST_PREFIX} {summary}"
            if summary
            else RESUME_STATE_LOST_PREFIX
        )
    elif reason == BlockedReason.VISUAL_ENGINE:
        # The controller's text can carry local paths; it stays in the server log.
        text = (
            "The visual engine on the test machine failed or was not ready. "
            "Check Advanced diagnostics on the Environment page."
        )
    elif isinstance(exc, BrowserUnavailableError):
        # ``safe_message`` is fixed server text; the runner's ``detail`` is log-only.
        text = exc.safe_message
    elif reason == BlockedReason.INTERNAL_ERROR:
        text = f"Unexpected server error ({type(exc).__name__}). See the server log."
    elif isinstance(exc, AIServiceError):
        text = exc.message
    else:
        text = str(exc) or type(exc).__name__
    return _truncate(text)


def fault_ask(reason: BlockedReason) -> str:
    return _ASK_BY_REASON.get(reason, _ASK_BY_REASON[BlockedReason.INTERNAL_ERROR])


def help_continue_mode(
    fault: BlockedReason,
    checkpoint: OrchestratorCheckpoint | None,
    *,
    restore_pending: bool,
) -> HelpContinueMode:
    """How a case paused by ``fault`` goes on; stored on its help request.

    ``restore_pending``: the case was resumed and its restore sub-task had not
    finished when the fault hit, so the earlier steps' state was never brought
    back and Continue restores it.
    """
    if checkpoint is None:
        return HelpContinueMode.FROM_SCRATCH
    if fault in _RESTORE_FAULTS or restore_pending:
        return HelpContinueMode.RESTORE
    return HelpContinueMode.CURRENT_SCREEN


def _truncate(text: str) -> str:
    if len(text) <= MAX_FAULT_MESSAGE_CHARS:
        return text
    return text[: MAX_FAULT_MESSAGE_CHARS - 1] + "…"
