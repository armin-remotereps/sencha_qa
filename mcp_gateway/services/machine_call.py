"""The round-trip block of one machine call that is not an agent tool (the
browse_* tools; docs/plan/feat-mcp-browser-toolset.md, MCP surface ›
machine_call).

Modelled on ``run_agent_tool`` (left as it is): one of the process's
round-trip slots (``machine_round_trip``), the project's machine lock
(``machine_lock``) for ``lock_seconds``, never a wait for a runner reconnect
(``no_reconnect_wait``) and a hard deadline on every reply wait and AI
request inside (``call_deadline``), so the call ends before its lock
expires. Everything ``work`` does runs under that one lock, so a list, an
AI pick and an action are one machine action. Exceptions are mapped by
``map_error`` (default ``map_machine_exception``); an unmapped one
propagates.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from mcp_gateway.exceptions import McpToolError
from mcp_gateway.services.machine_errors import (
    MachineErrorCode,
    machine_error,
    map_machine_exception,
)
from mcp_gateway.services.machine_round_trip import machine_lock, machine_round_trip
from projects.call_deadline import call_deadline
from projects.controller_reconnect_services import no_reconnect_wait

_T = TypeVar("_T")


def machine_call(
    project_id: int,
    *,
    deadline_seconds: int,
    lock_seconds: int,
    work: Callable[[], _T],
    map_error: Callable[[Exception], McpToolError | None] = map_machine_exception,
) -> _T:
    """Run ``work`` under the slot, lock, no-reconnect and deadline guards.
    Raises ``McpToolError`` (``[machine_busy]`` when the slot or lock is
    taken, or a mapped fault)."""
    busy = machine_error(MachineErrorCode.MACHINE_BUSY)
    try:
        with (
            machine_round_trip(busy=busy),
            machine_lock(project_id, lock_seconds, busy=busy),
            no_reconnect_wait(),
            call_deadline(deadline_seconds),
        ):
            return work()
    except McpToolError:
        raise
    except Exception as exc:
        mapped = map_error(exc)
        if mapped is None:
            raise
        raise mapped from exc
