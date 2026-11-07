"""Use case: the MCP tester reads the project's knowledge base.

``list_knowledge``, ``search_knowledge`` and ``read_knowledge`` run the same
read-only handlers the agents use (``dispatch_knowledge_tool``); their text is
already fenced as data and capped. A refused call becomes a tool error.
"""

from __future__ import annotations

from agents.types import ToolCall
from knowledge_base.services.agent_tools import (
    KNOWLEDGE_TOOL_NAMES,
    dispatch_knowledge_tool,
)
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.schemas import KnowledgeRequest, TextResponse
from mcp_gateway.services.call_guard import McpCall

_UNKNOWN_TOOL = "Unknown knowledge base tool."


def run_knowledge_tool(call: McpCall, request: KnowledgeRequest) -> TextResponse:
    if request.tool_name not in KNOWLEDGE_TOOL_NAMES:
        raise McpToolError(_UNKNOWN_TOOL)
    result = dispatch_knowledge_tool(
        ToolCall(
            tool_call_id="mcp",
            tool_name=request.tool_name,
            # Absent optional arguments are left out, as the agents send them.
            arguments={
                name: value
                for name, value in request.arguments.items()
                if value is not None
            },
        ),
        call.project.pk,
    )
    if result.is_error:
        raise McpToolError(result.content)
    return TextResponse(text=result.content)
