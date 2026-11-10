"""The gate of every machine tool: the caller's own live MCP claim, then the
test machine (docs/plan/feat-mcp-runner-tools.md, Gate).

The test machine is the runner of the claimed case's run
(``TestRun.runner``, read in the claim's one query;
docs/plan/feat-user-test-runners.md A16). Checks in order, all cheap,
nothing written:

1. The claim: the case is held by this caller's MCP claim and not paused
   (``own_claim``; one query, which also reads the run's runner id). The
   existing claim refusals.
2. The run has a test machine (``[not_set_up]``; a Manual run's runner is
   optional).
3. The test machine (one query, which also loads the capabilities and OS
   name the command tools need): connected (``[machine_offline]``), a
   phone's device ready (``[device_offline]``), and the tool exists for
   its target (``[wrong_target]``).
4. Nothing but a claimed case of this run holds the runner: another run, a
   Machine setup turn, a self-update or a readiness Scan / Check machine
   answer ``[machine_busy]`` (``run_machine.held_by_other``).
5. Tools that find elements by description on a screen parse need a ready
   visual engine worker (``[visual_engine_offline]``; one query).

``web_search`` and ``wait`` (``needs_runner`` False) run step 1 only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from controller_client.protocol import ClientCapability, RunnerTarget
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.machine_tools import MachineTool
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.machine_errors import MachineErrorCode, machine_error
from mcp_gateway.services.own_claim import NOT_YOUR_CLAIM, load_own_claim, refuse_paused
from mcp_gateway.services.run_machine import held_by_other
from projects.runner_capability_services import os_name_of
from projects.runner_target import runner_target_of
from runners.machine_state import device_offline
from runners.models import TestRunner
from runners.services.runner_access import runner_serves_project
from runners.types import RunnerId
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
    # The run's runner the tool drives; None for tools that need no test
    # machine.
    runner_id: RunnerId | None = None
    # From the gate's one runner read (empty for tools that need no test
    # machine): the capabilities the test machine advertised and the OS name
    # it reported, so the command tools read no runner row again.
    capabilities: frozenset[str] = frozenset()
    os_name: str = ""

    def supports(self, capability: ClientCapability) -> bool:
        """The test machine advertised ``capability`` (no query)."""
        return capability.value in self.capabilities

    def machine(self) -> RunnerId:
        """The runner of a tool that needs the test machine. Raises
        ``McpToolError`` (``[not_set_up]``) for a session opened without one,
        which the gate never hands to such a tool."""
        if self.runner_id is None:
            raise machine_error(MachineErrorCode.NOT_SET_UP)
        return self.runner_id


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
    test_run = claim.pivot.test_run
    if test_run.runner_id is None:
        raise machine_error(MachineErrorCode.NOT_SET_UP)
    runner = _ready_runner(RunnerId(test_run.runner_id), tool, project_id)
    if held_by_other(runner.runner_id, test_run.pk):
        raise machine_error(MachineErrorCode.MACHINE_BUSY)
    if tool.uses_visual_engine and get_visual_engine_status().ready_count == 0:
        raise machine_error(MachineErrorCode.VISUAL_ENGINE_OFFLINE)
    return MachineSession(
        project_id=project_id,
        pivot_id=pivot_id,
        claim_started_at=claim_started_at,
        runner_target=runner_target_of(runner),
        runner_id=runner.runner_id,
        capabilities=_capabilities(runner.capabilities),
        os_name=os_name_of(runner.system_info),
    )


def _ready_runner(
    runner_id: RunnerId, tool: MachineTool, project_id: int
) -> TestRunner:
    """The run's runner, if ``tool`` can run on it now (two queries; also
    loads what the command tools need). An archived runner, or one that no
    longer serves the project (``runner_serves_project``: its owner left or
    was deactivated), reads as none."""
    runner = (
        TestRunner.objects.filter(pk=runner_id, archived_at__isnull=True)
        .only(
            "owner_id",
            "archived_at",
            "connected",
            "system_info",
            "capabilities",
            "device_state",
            "device_reported_at",
        )
        .first()
    )
    if runner is None or not runner_serves_project(runner, project_id):
        raise machine_error(MachineErrorCode.NOT_SET_UP)
    if not runner.connected:
        raise machine_error(MachineErrorCode.MACHINE_OFFLINE)
    if device_offline(runner):
        raise machine_error(MachineErrorCode.DEVICE_OFFLINE)
    if runner_target_of(runner) not in tool.targets:
        raise machine_error(MachineErrorCode.WRONG_TARGET)
    return runner


def _capabilities(value: object) -> frozenset[str]:
    """A loaded ``capabilities`` value as a set of names."""
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))
