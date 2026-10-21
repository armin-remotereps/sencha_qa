"""Turn an infrastructure exception into a Blocked reason and a safe message.

Pure: no ORM and no import of ``projects.services``, so both case execution and
the TestRail push can use it.
"""

from __future__ import annotations

from celery.exceptions import SoftTimeLimitExceeded

from agents.exceptions import AIServiceError
from projects.exceptions import (
    ControllerReconnectTimeoutError,
    TestRunnerDisconnectedError,
    TestRunnerUnresponsiveError,
    VisualEngineError,
)
from projects.models import BlockedReason

MAX_BLOCKED_MESSAGE_CHARS = 500

_REASON_BY_TYPE: tuple[tuple[type[BaseException], BlockedReason], ...] = (
    (ControllerReconnectTimeoutError, BlockedReason.RUNNER_DISCONNECTED),
    (TestRunnerDisconnectedError, BlockedReason.RUNNER_DISCONNECTED),
    (TestRunnerUnresponsiveError, BlockedReason.RUNNER_UNRESPONSIVE),
    (VisualEngineError, BlockedReason.VISUAL_ENGINE),
    (AIServiceError, BlockedReason.AI_SERVICE),
    (SoftTimeLimitExceeded, BlockedReason.TIME_LIMIT),
)


def classify_case_exception(exc: BaseException) -> BlockedReason:
    for exc_type, reason in _REASON_BY_TYPE:
        if isinstance(exc, exc_type):
            return reason
    return BlockedReason.INTERNAL_ERROR


def blocked_message(exc: BaseException, reason: BlockedReason) -> str:
    """User-facing text: shown in the UI and pushed to TestRail comments.

    ``AIServiceError.message`` is built by the LLM client without URLs, headers
    or keys. Visual-engine and unexpected errors show fixed text, never the
    exception's, which may contain local paths or anything else.
    """
    if reason == BlockedReason.TIME_LIMIT:
        text = "The case ran past its time limit before the agent reached a verdict."
    elif reason == BlockedReason.VISUAL_ENGINE:
        # The controller's text can carry local paths; it stays in the server log.
        text = (
            "The visual engine on the test machine failed or was not ready. "
            "Check Advanced diagnostics on the Environment page."
        )
    elif reason == BlockedReason.INTERNAL_ERROR:
        text = f"Unexpected server error ({type(exc).__name__}). See the server log."
    elif isinstance(exc, AIServiceError):
        text = exc.message
    else:
        text = str(exc) or type(exc).__name__
    return _truncate(text)


def _truncate(text: str) -> str:
    if len(text) <= MAX_BLOCKED_MESSAGE_CHARS:
        return text
    return text[: MAX_BLOCKED_MESSAGE_CHARS - 1] + "…"
