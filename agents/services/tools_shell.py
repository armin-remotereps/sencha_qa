from __future__ import annotations

import logging

from agents.services.ssh_session import SSHSessionManager
from agents.types import ToolResult

logger = logging.getLogger(__name__)


def execute_command(ssh_session: SSHSessionManager, *, command: str) -> ToolResult:
    """Execute a shell command via the persistent SSH session."""
    try:
        result = ssh_session.execute(command)
        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"STDERR: {result.stderr}")
        output_parts.append(f"Exit code: {result.exit_code}")
        content = "\n".join(output_parts)
        return ToolResult(
            tool_call_id="",
            content=content,
            is_error=result.exit_code != 0,
        )
    except Exception as e:
        logger.error("SSH command execution failed: %s", e)
        return ToolResult(
            tool_call_id="",
            content=f"SSH error: {e}",
            is_error=True,
        )
