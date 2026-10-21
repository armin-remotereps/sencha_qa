from __future__ import annotations

import logging
from typing import Callable

from agents.types import AgentCancelledError, ToolResult
from projects.exceptions import ControllerReconnectTimeoutError

logger = logging.getLogger(__name__)


def safe_tool_call(
    operation: str,
    fn: Callable[[], ToolResult],
) -> ToolResult:
    try:
        return fn()
    except (AgentCancelledError, ControllerReconnectTimeoutError):
        # Cancellation and a lost Test Runner must stop the whole agent loop,
        # not just this tool call (mirrors how
        # agents/services/tools_utility.py:wait lets cancellation propagate).
        raise
    except Exception as e:
        logger.error("%s failed: %s", operation, e)
        return ToolResult(
            tool_call_id="",
            content=f"{operation} error: {e}",
            is_error=True,
        )
