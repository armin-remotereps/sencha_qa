"""The send gates every new setup turn shares, besides the live-turn one.

``send_message`` (a member's message) and ``action_approval_decision`` (an
approval that queues a turn) both call ``run_and_update_refusal``; only help,
result and auto-fix threads add the machine READY gate (Q3: the main chat
may run with the Test Runner offline).

A manual case discussion has its own gates (``manual_thread_send_refusal``):
its case must still be claimed, its own started manual run does not refuse
it, and it skips the READY gate (``run_setup_task`` checks at call time).
"""

from __future__ import annotations

from datetime import datetime

from machine_setup.exceptions import (
    MachineSetupError,
    SetupMachineNotReadyError,
    SetupRunActiveError,
)
from projects.manual_run_policy import (
    MANUAL_DISCUSS_NOT_CLAIMED,
    is_claimed_manual_case,
)
from projects.models import Project, TestRun, TestRunStatus, TestRunTestCase
from projects.runner_update_state import (
    RUNNER_UPDATE_RUNNING_MESSAGE,
    runner_update_in_progress,
)
from projects.services import MachineState, get_machine_state
from visual_engine.services.pool_status import get_visual_engine_status


def run_and_update_refusal(
    project: Project, now: datetime, *, own_manual_run_id: int | None = None
) -> MachineSetupError | None:
    """A Started run or a Test Runner update refuses a new turn (two queries).

    ``own_manual_run_id``: a manual case discussion's own started manual run,
    which does not refuse it (any other started run still does).
    The caller has already resolved the live turn under the project row lock.
    """
    started = TestRun.objects.filter(project=project, status=TestRunStatus.STARTED)
    if own_manual_run_id is not None:
        started = started.exclude(pk=own_manual_run_id)
    if started.exists():
        return SetupRunActiveError()
    if runner_update_in_progress(project.pk, now):
        return MachineSetupError(RUNNER_UPDATE_RUNNING_MESSAGE)
    return None


def thread_send_refusal(
    project: Project, now: datetime, *, main_chat: bool
) -> MachineSetupError | None:
    """Every send gate but the live-turn one, in ``send_setup_message``'s order.

    Help, result and auto-fix threads add the READY gate; the main chat does
    not (Q3: its machine tools say "not connected"). Also read, without
    sending, by the Test Runner window's discussion (``runner_discussion``).
    """
    refusal = run_and_update_refusal(project, now)
    if refusal is not None or main_chat:
        return refusal
    return machine_not_ready_refusal(project)


def machine_not_ready_refusal(project: Project) -> MachineSetupError | None:
    """Help, result and auto-fix threads: the runner connected and the visual
    engine ready (``project.agent_connected`` must be fresh; one query)."""
    if get_machine_state(project, get_visual_engine_status()) != MachineState.READY:
        return SetupMachineNotReadyError()
    return None


def manual_thread_send_refusal(
    project: Project, now: datetime, *, pivot_id: int | None
) -> MachineSetupError | None:
    """Every send gate of a manual case discussion but the live-turn one.

    Refused (``MANUAL_DISCUSS_NOT_CLAIMED``) once the case is no longer a
    claimed IN_PROGRESS case of a started manual run of ``project``; the
    case's own run is exempt from the started-run refusal; no READY gate.
    Three queries. Also read, without sending, by the thread page (the Test
    Runner window, which already knows the claim, uses
    ``run_and_update_refusal`` directly).
    """
    run_id = claimed_manual_run_id(project, pivot_id)
    if run_id is None:
        return MachineSetupError(MANUAL_DISCUSS_NOT_CLAIMED)
    return run_and_update_refusal(project, now, own_manual_run_id=run_id)


def claimed_manual_run_id(project: Project, pivot_id: int | None) -> int | None:
    """The run of ``pivot_id`` when it is a claimed IN_PROGRESS case of a
    started manual run of ``project``; else None (one query)."""
    if pivot_id is None:
        return None
    pivot = (
        TestRunTestCase.objects.select_related("test_run")
        .only(
            "id",
            "status",
            "manual_claim_token",
            "test_run__id",
            "test_run__status",
            "test_run__help_mode",
        )
        .filter(pk=pivot_id, test_run__project=project)
        .first()
    )
    if pivot is None or not is_claimed_manual_case(pivot.test_run, pivot):
        return None
    return pivot.test_run_id
