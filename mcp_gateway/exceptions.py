"""Domain errors of the MCP gateway; ``message`` is safe to return to the
MCP client (it never names the key, the project internals or a user)."""

from __future__ import annotations


class McpToolError(Exception):
    """A refused tool call; ``message`` is the tool error text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
