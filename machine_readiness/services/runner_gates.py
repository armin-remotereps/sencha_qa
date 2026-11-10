"""When the scan and the quick check may send actions to a Test Runner.

Shared by ``request_scan``, ``scan_artifacts``, ``request_quick_check`` and
``quick_check`` so the request (view) and the worker apply the same gates.
The busy gate comes through ``RunnerPorts`` (plan D2). The scan and the check
need a runner: the run's on the run card, the one picked on the Machine
readiness tab, the setup turn's in the main chat.
"""

from __future__ import annotations

from machine_readiness.runner_ports import ReadinessWork, RunnerBusy, RunnerPorts
from runners.types import RunnerId

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
OTHER_PROJECT_MESSAGE = "The Test Runner is busy with another run."
SCANNING_MESSAGE = (
    "The Test Runner is scanning the artifacts folder. Try again when the "
    "scan has finished."
)
CHECKING_MESSAGE = (
    "The Test Runner is checking the machine. Try again when the check has " "finished."
)
NOT_CONNECTED_MESSAGE = "The Test Runner is not connected."
NOT_SERVING_MESSAGE = (
    "This Test Runner is no longer available to this project. Choose another "
    "one, then try again."
)
NO_RUNNER_MESSAGE = "No Test Runner selected. Pick one, then try again."

_BUSY_MESSAGES: dict[RunnerBusy, str] = {
    RunnerBusy.NOT_SERVING: NOT_SERVING_MESSAGE,
    RunnerBusy.RUN_STARTED: RUN_STARTED_MESSAGE,
    RunnerBusy.MANUAL_TESTER: MANUAL_TESTER_MESSAGE,
    RunnerBusy.SETUP_RUNNING: SETUP_RUNNING_MESSAGE,
    RunnerBusy.UPDATING: RUNNER_UPDATING_MESSAGE,
    RunnerBusy.OTHER_PROJECT: OTHER_PROJECT_MESSAGE,
    RunnerBusy.SCANNING: SCANNING_MESSAGE,
    RunnerBusy.CHECKING: CHECKING_MESSAGE,
}


def runner_action_refusal(
    *,
    project_id: int,
    runner_id: RunnerId | None,
    ports: RunnerPorts,
    connected: bool,
    turn_id: int | None,
    work: ReadinessWork,
) -> str | None:
    """Why the runner may not be used now, or None. A few cheap queries.

    Refused with no runner, while an agent-driven run is Started on it, a
    tester performs a claimed, unpaused manual case on it, a setup turn
    drives it (unless the caller is that turn: ``turn_id``), it is updating,
    a paused run of another project holds it, the other readiness action
    (``work``: a scan and a check never overlap on one runner) holds it, or
    it is not connected.
    """
    if runner_id is None:
        return NO_RUNNER_MESSAGE
    busy = ports.runner_busy(runner_id, project_id, work=work, ignore_turn_id=turn_id)
    if busy is not None:
        return _BUSY_MESSAGES[busy]
    if not connected:
        return NOT_CONNECTED_MESSAGE
    return None
