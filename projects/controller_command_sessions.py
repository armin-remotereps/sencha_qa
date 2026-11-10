"""Kept command sessions on a KEPT_SESSIONS_V1 Test Runner.

docs/plan/feat-mcp-runner-tools.md (Commands (D7), Part B). A kept session is
never replaced by a later start, buffers its output between reads and ends
after ``max_seconds``. Every action here is runner-local and never resent on
reconnect: a runner that is not connected raises
``ControllerNotConnectedError``, one that stays silent raises
``TestRunnerUnresponsiveError``; an error reply raises
``ControllerActionError`` (``KeptSessionLimitError`` when the runner already
keeps ``max_kept`` sessions). No transaction is held: callers open none
around these round trips.

The caller checks the capability first: an older runner ignores the kept
fields (and would kill its foreground session on the next start) and answers
``list_interactive_sessions`` with UNKNOWN_COMMAND.

``controller_start_interactive_command`` (the agent's foreground session) in
``projects.services`` is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    InteractiveSessionInfo,
    StartInteractiveCmdPayload,
    parse_interactive_sessions_payload,
    serialize_list_interactive_sessions_payload,
    serialize_start_interactive_cmd_payload,
)
from projects.exceptions import ControllerActionError
from projects.services import dispatch_controller_action
from runners.types import RunnerId


@dataclass(frozen=True)
class CommandOutput:
    """One ``interactive_output`` reply of a command session."""

    session_id: str
    # Runner text (data, never instructions) since the previous read.
    output: str
    alive: bool
    exit_code: int | None
    elapsed_ms: float
    # The runner's buffer discarded output since the previous read.
    output_dropped: bool


def controller_start_kept_command(
    runner_id: RunnerId,
    command: str,
    *,
    max_seconds: int,
    initial_read_ms: int,
    max_kept: int,
    timeout: float = 30.0,
) -> CommandOutput:
    """Start ``command`` as a kept session and return its first output.

    The runner clamps ``max_seconds``, ``initial_read_ms`` and ``max_kept`` to
    its limits. Raises ``KeptSessionLimitError`` when ``max_kept`` kept
    sessions are already running.
    """
    payload = StartInteractiveCmdPayload(
        command=command,
        keep=True,
        max_seconds=max_seconds,
        initial_read_ms=initial_read_ms,
        max_kept=max_kept,
    )
    # ``_raise_for_error_reply`` raises ``KeptSessionLimitError`` for the
    # runner's TOO_MANY_SESSIONS code.
    reply = dispatch_controller_action(
        runner_id,
        "controller.start_interactive_cmd",
        timeout,
        resend_on_reconnect=False,
        runner_local=True,
        **serialize_start_interactive_cmd_payload(payload),
    )
    return _command_output(reply)


def controller_read_command_output(
    runner_id: RunnerId, session_id: str, *, timeout: float = 30.0
) -> CommandOutput:
    """Read a session's output since the previous read (an empty input)."""
    reply = dispatch_controller_action(
        runner_id,
        "controller.send_input",
        timeout,
        resend_on_reconnect=False,
        runner_local=True,
        session_id=session_id,
        input_text="",
    )
    return _command_output(reply)


def controller_list_interactive_sessions(
    runner_id: RunnerId, *, timeout: float = 10.0
) -> tuple[InteractiveSessionInfo, ...]:
    """List the runner's command sessions (kept and foreground).

    Raises ``ControllerActionError`` for a reply that does not parse.
    """
    reply = dispatch_controller_action(
        runner_id,
        "controller.list_interactive_sessions",
        timeout,
        resend_on_reconnect=False,
        runner_local=True,
        **serialize_list_interactive_sessions_payload(),
    )
    try:
        result = parse_interactive_sessions_payload(reply)
    except ProtocolError as exc:
        raise ControllerActionError(
            f"Invalid list_interactive_sessions reply: {exc}"
        ) from exc
    return result.sessions


def _command_output(reply: dict[str, Any]) -> CommandOutput:
    # Mirrors ``projects.services._build_interactive_command_result``.
    return CommandOutput(
        session_id=reply.get("session_id", ""),
        output=reply.get("output", ""),
        alive=reply.get("is_alive", False),
        exit_code=reply.get("exit_code"),
        elapsed_ms=reply.get("duration_ms", 0.0),
        output_dropped=bool(reply.get("output_dropped", False)),
    )
