from __future__ import annotations

from agents.services.ssh_session import SSHSessionManager
from agents.services.tool_utils import safe_tool_call
from agents.types import ToolResult


def _wrap_background_command(command: str) -> str:
    """Wrap a backgrounded command with nohup and output redirection."""
    core = command.rstrip()[:-1].rstrip()
    return f"nohup {core} > /dev/null 2>&1 & echo 'Background PID:' $!"


def execute_command(ssh_session: SSHSessionManager, *, command: str) -> ToolResult:
    def _do() -> ToolResult:
        actual_command = command
        if command.rstrip().endswith("&"):
            actual_command = _wrap_background_command(command)
        result = ssh_session.execute(actual_command)
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

    return safe_tool_call("SSH command execution", _do)
