"""When the scan and the quick check may send actions to the Test Runner.

Shared by ``request_scan``, ``scan_artifacts`` and ``quick_check`` so the
request (view) and the worker apply the same gates. The setup-turn and
runner-update gates come through ``RunnerPorts`` (plan D2).
"""

from __future__ import annotations

from django.utils import timezone

from machine_readiness.runner_ports import RunnerPorts
from projects.models import TestRun, TestRunStatus

RUN_STARTED_MESSAGE = (
    "A run is executing on this machine. Try again when it has finished."
)
SETUP_RUNNING_MESSAGE = (
    "Machine setup is working on this machine. Try again when its turn has finished."
)
RUNNER_UPDATING_MESSAGE = (
    "The Test Runner is being updated. Wait for the update to finish."
)
NOT_CONNECTED_MESSAGE = "The Test Runner is not connected."


def runner_action_refusal(
    *, project_id: int, ports: RunnerPorts, connected: bool, during_turn: bool
) -> str | None:
    """Why the runner may not be used now, or None. Three queries at most.

    Refused while a run is Started, a setup turn is live (unless the caller
    is that turn: ``during_turn``), the runner is updating, or no runner is
    connected.
    """
    now = timezone.now()
    if TestRun.objects.filter(
        project_id=project_id, status=TestRunStatus.STARTED
    ).exists():
        return RUN_STARTED_MESSAGE
    if not during_turn and ports.live_turn_exists(project_id, now):
        return SETUP_RUNNING_MESSAGE
    if ports.runner_update_in_progress(project_id, now):
        return RUNNER_UPDATING_MESSAGE
    if not connected:
        return NOT_CONNECTED_MESSAGE
    return None
