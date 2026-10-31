"""The send gates every new setup turn shares, besides the live-turn one.

``send_message`` (a member's message) and ``action_approval_decision`` (an
approval that queues a turn) both call ``run_and_update_refusal``; only help,
result and auto-fix threads add the machine READY gate (Q3: the main chat
may run with the Test Runner offline).
"""

from __future__ import annotations

from datetime import datetime

from machine_setup.exceptions import (
    MachineSetupError,
    SetupMachineNotReadyError,
    SetupRunActiveError,
)
from projects.models import Project, TestRun, TestRunStatus
from projects.runner_update_state import (
    RUNNER_UPDATE_RUNNING_MESSAGE,
    runner_update_in_progress,
)
from projects.services import MachineState, get_machine_state
from visual_engine.services.pool_status import get_visual_engine_status


def run_and_update_refusal(project: Project, now: datetime) -> MachineSetupError | None:
    """A Started run or a Test Runner update refuses a new turn (two queries).

    The caller has already resolved the live turn under the project row lock.
    """
    if TestRun.objects.filter(project=project, status=TestRunStatus.STARTED).exists():
        return SetupRunActiveError()
    if runner_update_in_progress(project.pk, now):
        return MachineSetupError(RUNNER_UPDATE_RUNNING_MESSAGE)
    return None


def machine_not_ready_refusal(project: Project) -> MachineSetupError | None:
    """Help, result and auto-fix threads: the runner connected and the visual
    engine ready (``project.agent_connected`` must be fresh; one query)."""
    if get_machine_state(project, get_visual_engine_status()) != MachineState.READY:
        return SetupMachineNotReadyError()
    return None
