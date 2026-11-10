"""The send gates every new setup turn shares, besides the live-turn one.

``send_message`` (a member's message), ``action_approval_decision`` (an
approval that queues a turn) and the auto-fix start all ask
``runner_refusal`` about the thread's Test Runner (``thread_runner``), under
its row lock: a runner that no longer serves the project (archived, owner
deactivated or no longer a writable member), a Started agent run on that
runner, a self-update, a readiness scan or check, or a holder of another
project refuses. A thread with no
runner has nothing to refuse there. Only help, result and auto-fix threads
add the machine READY gate (Q3: the main chat may run with its Test Runner
offline or with none picked; its machine tools say so).

A Started Manual run does not refuse: its tester is kept out of the agent's
way by the call-time ``run_setup_task`` gate (``setup_tools``) and by claim
/ Continue refusing while a turn is live.

A manual case discussion has its own gates (``manual_thread_send_refusal``):
its case must still be claimed, and it skips the READY gate
(``run_setup_task`` checks at call time).

A case analysis thread (``triage_thread_send_refusal``) needs no machine, so
it skips the runner gates; once its run is deleted it is read-only.
"""

from __future__ import annotations

from machine_setup.exceptions import MachineSetupError
from machine_setup.services.thread_runner import (
    RUN_RUNNER_UNAVAILABLE_MESSAGE,
    RUNNER_UNAVAILABLE_MESSAGE,
    machine_not_ready_refusal,
    runner_hold_refusal,
    runner_usable_by_project,
)
from projects.manual_run_policy import (
    MANUAL_DISCUSS_NOT_CLAIMED,
    is_claimed_manual_case,
)
from projects.models import Project, TestRunTestCase
from runners.models import TestRunner

TRIAGE_RUN_GONE_MESSAGE = (
    "The run of this analysis was deleted, so this chat is read-only."
)


def runner_refusal(
    runner: TestRunner | None, project_id: int, *, main_chat: bool
) -> MachineSetupError | None:
    """Why a new turn may not drive ``runner`` now (``thread_runner``); None
    for no runner. Call under the runner row lock.

    First whether the runner still serves the project (not archived, its
    owner an active writable member: ``runner_usable_by_project``, one
    query), worded for the main chat's pick or the run's runner
    (``main_chat``), then its holders (``runner_hold_refusal``).
    """
    if runner is None:
        return None
    if not runner_usable_by_project(runner, project_id):
        return MachineSetupError(
            RUNNER_UNAVAILABLE_MESSAGE if main_chat else RUN_RUNNER_UNAVAILABLE_MESSAGE
        )
    return runner_hold_refusal(runner.runner_id, project_id)


def thread_send_refusal(
    project: Project, runner: TestRunner | None, *, kind: str, main_chat: bool
) -> MachineSetupError | None:
    """Every send gate but the live-turn one, in ``send_setup_message``'s order.

    Help, result and auto-fix threads add the READY gate; the main chat does
    not (Q3: its machine tools say "not connected"). Also read, without
    sending, by the Test Runner window's discussion (``runner_discussion``).
    """
    refusal = runner_refusal(runner, project.pk, main_chat=main_chat)
    if refusal is not None or main_chat:
        return refusal
    return machine_not_ready_refusal(runner, kind)


def manual_thread_send_refusal(
    project: Project, runner: TestRunner | None, *, pivot_id: int | None
) -> MachineSetupError | None:
    """Every send gate of a manual case discussion but the live-turn one.

    Refused (``MANUAL_DISCUSS_NOT_CLAIMED``) once the case is no longer a
    claimed IN_PROGRESS case of a started manual run of ``project``; no
    READY gate. Also read, without sending, by the thread page.
    """
    if not is_claimed_manual_pivot(project, pivot_id):
        return MachineSetupError(MANUAL_DISCUSS_NOT_CLAIMED)
    return runner_refusal(runner, project.pk, main_chat=False)


def is_claimed_manual_pivot(project: Project, pivot_id: int | None) -> bool:
    """Whether ``pivot_id`` is a claimed IN_PROGRESS case of a started manual
    run of ``project``, claimed at the Test Runner window (one query)."""
    if pivot_id is None:
        return False
    pivot = (
        TestRunTestCase.objects.select_related("test_run")
        .only(
            "id",
            "status",
            "manual_claim_token",
            "manual_claim_source",
            "test_run__id",
            "test_run__status",
            "test_run__help_mode",
        )
        .filter(pk=pivot_id, test_run__project=project)
        .first()
    )
    return (
        pivot is not None
        and is_claimed_manual_case(pivot.test_run, pivot)
        and pivot.manual_claim_at_runner
    )


def triage_thread_send_refusal(
    *, triage_run_id: int | None
) -> MachineSetupError | None:
    """Every send gate of a case analysis thread but the live-turn one.

    No runner gates (the analysis never uses the machine); a thread whose
    run was deleted is read-only.
    """
    if triage_run_id is None:
        return MachineSetupError(TRIAGE_RUN_GONE_MESSAGE)
    return None
