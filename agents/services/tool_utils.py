from __future__ import annotations

import logging
from typing import Callable

from celery.exceptions import SoftTimeLimitExceeded

from agents.exceptions import InfrastructureError
from agents.types import AgentCancelledError, ToolResult

logger = logging.getLogger(__name__)


def safe_tool_call(
    operation: str,
    fn: Callable[[], ToolResult],
) -> ToolResult:
    try:
        return fn()
    except (AgentCancelledError, InfrastructureError, SoftTimeLimitExceeded):
        # Cancellation, infrastructure faults (lost or unresponsive Test Runner,
        # Visual engine, AI service; ControllerReconnectTimeoutError included)
        # and the case time limit must stop the whole agent loop, not just this
        # tool call, so the case ends Cancelled or pauses for help instead of
        # the agent judging the app on a fault that is not the app's (mirrors
        # how agents/services/tools_utility.py:wait lets cancellation propagate).
        raise
    except Exception as e:
        logger.error("%s failed: %s", operation, e)
        return ToolResult(
            tool_call_id="",
            content=f"{operation} error: {e}",
            is_error=True,
        )
