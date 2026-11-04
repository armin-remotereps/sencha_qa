"""The send gates every new setup turn shares, besides the live-turn one.

``send_message`` (a member's message) and ``action_approval_decision`` (an
approval that queues a turn) both call ``run_and_update_refusal``; only help,
result and auto-fix threads add the machine READY gate (Q3: the main chat
may run with the Test Runner offline).

Only an agent-driven Started run refuses (``run_machine_use``): a Manual
run's tester is kept out of the agent's way by the call-time
``run_setup_task`` gate (``setup_tools``) and by claim / Continue refusing
while a turn is live.

A manual case discussion has its own gates (``manual_thread_send_refusal``):
its case must still be claimed, and it skips the READY gate
(``run_setup_task`` checks at call time).

A case analysis thread (``triage_thread_send_refusal``) needs no machine, so
it skips the READY gate too; once its run is deleted it is read-only.
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
from projects.models import Project, TestRunTestCase
from projects.run_machine_use import agent_run_started
from projects.runner_update_state import (
    RUNNER_UPDATE_RUNNING_MESSAGE,
    runner_update_in_progress,
)
from projects.services import MachineState, get_machine_state
from visual_engine.services.pool_status import get_visual_engine_status

TRIAGE_RUN_GONE_MESSAGE = (
    "The run of this analysis was deleted, so this chat is read-only."
)


def run_and_update_refusal(project: Project, now: datetime) -> MachineSetupError | None:
    """An agent-driven Started run or a Test Runner update refuses a new turn
    (two queries; a Started Manual run does not).

    The caller has already resolved the live turn under the project row lock.
    """
    if agent_run_started(project.pk):
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
    claimed IN_PROGRESS case of a started manual run of ``project``; no
    READY gate (a Started Manual run does not refuse, ``run_machine_use``).
    Three queries. Also read, without sending, by the thread page (the Test
    Runner window, which already knows the claim, uses
    ``run_and_update_refusal`` directly).
    """
    if not is_claimed_manual_pivot(project, pivot_id):
        return MachineSetupError(MANUAL_DISCUSS_NOT_CLAIMED)
    return run_and_update_refusal(project, now)


def is_claimed_manual_pivot(project: Project, pivot_id: int | None) -> bool:
    """Whether ``pivot_id`` is a claimed IN_PROGRESS case of a started manual
    run of ``project`` (one query)."""
    if pivot_id is None:
        return False
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
    return pivot is not None and is_claimed_manual_case(pivot.test_run, pivot)


def triage_thread_send_refusal(
    project: Project, now: datetime, *, triage_run_id: int | None
) -> MachineSetupError | None:
    """Every send gate of a case analysis thread but the live-turn one.

    No READY gate (the analysis never uses the machine); an agent-driven
    Started run and a Test Runner update still refuse. A thread whose run
    was deleted is read-only. Two queries.
    """
    if triage_run_id is None:
        return MachineSetupError(TRIAGE_RUN_GONE_MESSAGE)
    return run_and_update_refusal(project, now)
