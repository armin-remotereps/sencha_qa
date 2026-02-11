from __future__ import annotations

import logging
from typing import Callable

from agents.types import ToolResult

logger = logging.getLogger(__name__)


def safe_tool_call(
    operation: str,
    fn: Callable[[], ToolResult],
) -> ToolResult:
    try:
        return fn()
    except Exception as e:
        logger.error("%s failed: %s", operation, e)
        return ToolResult(
            tool_call_id="",
            content=f"{operation} error: {e}",
            is_error=True,
        )
