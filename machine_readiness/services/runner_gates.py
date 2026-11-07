"""When the scan and the quick check may send actions to the Test Runner.

Shared by ``request_scan``, ``scan_artifacts`` and ``quick_check`` so the
request (view) and the worker apply the same gates. The run, setup-turn and
runner-update gates come through ``RunnerPorts`` (plan D2).
"""

from __future__ import annotations

from django.utils import timezone

from machine_readiness.runner_ports import RunnerPorts

RUN_STARTED_MESSAGE = (
    "A run is executing on this machine. Try again when it has finished."
)
SETUP_RUNNING_MESSAGE = (
    "Machine setup is working on this machine. Try again when its turn has finished."
)
RUNNER_UPDATING_MESSAGE = (
    "The Test Runner is being updated. Wait for the update to finish."
)
# Same wording as ``projects.manual_run_policy.MANUAL_TESTER_ON_MACHINE``.
MANUAL_TESTER_MESSAGE = (
    "A tester is performing a manual case on this machine. Try again once "
    "the case is paused, handed in or released."
)
NOT_CONNECTED_MESSAGE = "The Test Runner is not connected."


def runner_action_refusal(
    *, project_id: int, ports: RunnerPorts, connected: bool, during_turn: bool
) -> str | None:
    """Why the runner may not be used now, or None. Four queries at most.

    Refused while an agent-driven run is Started, a tester performs a claimed,
    unpaused manual case, a setup turn is live (unless the caller is that
    turn: ``during_turn``), the runner is updating, or no runner is
    connected.
    """
    now = timezone.now()
    if ports.agent_run_started(project_id):
        return RUN_STARTED_MESSAGE
    if ports.manual_tester_on_machine(project_id):
        return MANUAL_TESTER_MESSAGE
    if not during_turn and ports.live_turn_exists(project_id, now):
        return SETUP_RUNNING_MESSAGE
    if ports.runner_update_in_progress(project_id, now):
        return RUNNER_UPDATING_MESSAGE
    if not connected:
        return NOT_CONNECTED_MESSAGE
    return None
