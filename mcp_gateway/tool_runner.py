"""Shared plumbing of the MCP tool handlers (``mcp_app``, ``machine_handlers``).

``current_caller`` reads the verified bearer of this request;
``run_project_tool`` runs one project-scoped use case through
``call_guard.guarded_call`` in a worker thread and turns an ``McpToolError``
into the SDK's tool error; ``validation_text`` is the client-facing text of a
request DTO that failed validation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, TypeVar

from channels.db import database_sync_to_async
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from mcp_gateway.exceptions import McpToolError
from mcp_gateway.oauth_types import McpCaller
from mcp_gateway.services.authenticate_bearer import caller_of
from mcp_gateway.services.call_guard import CallKind, McpCall, guarded_call

_T = TypeVar("_T")

_INVALID_ARGUMENTS: Final = "[invalid] The arguments are not valid."
_NO_CALLER: Final = "Your access is not valid any more. Reconnect."


def current_caller() -> McpCaller:
    """The verified bearer of this request (a tool error when there is none)."""
    caller = caller_of(get_access_token())
    if caller is None:
        raise ToolError(_NO_CALLER)
    return caller


async def run_project_tool(
    tool: str,
    kind: CallKind,
    project_id: int,
    pivot_id: int | None,
    use: Callable[[McpCall], _T],
    *,
    session: str | None = None,
) -> _T:
    """Run one use case on the project the call names (worker thread)."""
    caller = current_caller()
    try:
        return await database_sync_to_async(guarded_call, thread_sensitive=False)(
            caller=caller,
            project_id=project_id,
            tool=tool,
            kind=kind,
            pivot_id=pivot_id,
            run=use,
            session=session,
        )
    except McpToolError as exc:
        raise ToolError(exc.message) from exc


def validation_text(exc: ValidationError) -> str:
    """The request's own refusal (``[invalid] ...``) or a generic one."""
    for item in exc.errors():
        ctx = item.get("ctx") or {}
        reason = ctx.get("error")
        if isinstance(reason, ValueError) and str(reason).startswith("[invalid]"):
            return str(reason)
    return _INVALID_ARGUMENTS
