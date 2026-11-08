"""The gate of every machine tool: the caller's own live MCP claim, then the
test machine (docs/plan/feat-mcp-runner-tools.md, Gate).

Checks in order, all cheap, nothing written:

1. The claim: the case is held by this caller's MCP claim and not paused
   (``own_claim``; one query). The existing claim refusals.
2. The test machine (one query, which also loads the capabilities and OS
   name the command tools need): connected (``[machine_offline]``), a
   phone's device ready (``[device_offline]``), and the tool exists for
   its target (``[wrong_target]``).
3. No readiness Scan or Check machine is running (``[machine_busy]``; two
   cache reads): a live claim keeps every other user off the machine except
   one already running when the claim was made.
4. Tools that find elements by description on a screen parse need a ready
   visual engine worker (``[visual_engine_offline]``; one query).

``web_search`` and ``wait`` (``needs_runner`` False) run step 1 only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from controller_client.protocol import ClientCapability, RunnerTarget
from machine_readiness.project_locks import (
    QUICK_CHECK_LOCK_PREFIX,
    SCAN_LOCK_PREFIX,
    is_locked,
)
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.machine_tools import MachineTool
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.machine_errors import MachineErrorCode, machine_error
from mcp_gateway.services.own_claim import NOT_YOUR_CLAIM, load_own_claim, refuse_paused
from projects.models import Project
from projects.runner_capability_services import os_name_of
from projects.runner_target import device_offline, project_runner_target
from visual_engine.services.pool_status import get_visual_engine_status


@dataclass(frozen=True)
class MachineSession:
    """A machine tool call allowed inside the caller's live MCP claim."""

    project_id: int
    pivot_id: int
    # The claim's ``manual_claimed_at``: identifies this claim (a later
    # claim of the same case has another).
    claim_started_at: datetime
    # The test machine's target; None for tools that need no test machine.
    runner_target: RunnerTarget | None
    # From the gate's one Project read (empty for tools that need no test
    # machine): the capabilities the test machine advertised and the OS name
    # it reported, so the command tools read no Project row again.
    capabilities: frozenset[str] = frozenset()
    os_name: str = ""

    def supports(self, capability: ClientCapability) -> bool:
        """The test machine advertised ``capability`` (no query)."""
        return capability.value in self.capabilities


def open_machine_session(
    call: McpCall, *, pivot_id: int, claim_token: str, tool: MachineTool
) -> MachineSession:
    """Run the gate for ``tool``. Raises ``McpToolError``."""
    project_id = call.project.pk
    claim = load_own_claim(
        project_id=project_id, pivot_id=pivot_id, claim_token=claim_token, lock=False
    )
    refuse_paused(claim)
    claim_started_at = claim.pivot.manual_claimed_at
    if claim_started_at is None:
        # A live claim always has its start time; treat a row without one as
        # not claimed.
        raise McpToolError(NOT_YOUR_CLAIM)
    if not tool.needs_runner:
        return MachineSession(
            project_id=project_id,
            pivot_id=pivot_id,
            claim_started_at=claim_started_at,
            runner_target=None,
        )
    project = _ready_project(project_id, tool)
    if is_locked(SCAN_LOCK_PREFIX, project_id) or is_locked(
        QUICK_CHECK_LOCK_PREFIX, project_id
    ):
        raise machine_error(MachineErrorCode.MACHINE_BUSY)
    if tool.uses_visual_engine and get_visual_engine_status().ready_count == 0:
        raise machine_error(MachineErrorCode.VISUAL_ENGINE_OFFLINE)
    return MachineSession(
        project_id=project_id,
        pivot_id=pivot_id,
        claim_started_at=claim_started_at,
        runner_target=project_runner_target(project),
        capabilities=_capabilities(project.agent_capabilities),
        os_name=os_name_of(project.agent_system_info),
    )


def _ready_project(project_id: int, tool: MachineTool) -> Project:
    """The project row, if ``tool`` can run on its connected test machine now
    (one query; also loads what the command tools need)."""
    project = Project.objects.only(
        "agent_connected",
        "agent_system_info",
        "agent_capabilities",
        "agent_device_state",
        "agent_device_reported_at",
    ).get(pk=project_id)
    if not project.agent_connected:
        raise machine_error(MachineErrorCode.MACHINE_OFFLINE)
    if device_offline(project):
        raise machine_error(MachineErrorCode.DEVICE_OFFLINE)
    if project_runner_target(project) not in tool.targets:
        raise machine_error(MachineErrorCode.WRONG_TARGET)
    return project


def _capabilities(value: object) -> frozenset[str]:
    """A loaded ``agent_capabilities`` value as a set of names."""
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))
