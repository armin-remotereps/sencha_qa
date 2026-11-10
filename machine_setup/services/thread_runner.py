"""Which Test Runner a Machine setup thread drives, and the runner gates.

The one definition of a thread's runner (plan › machine_setup, Runner per
thread):

- the main chat: ``SetupConversation.runner``, picked in the chat header;
- help, auto-fix, result and manual case threads: their run's current
  runner (``TestRun.runner``), so a reassigned run moves its chats along;
- case analysis threads: none (they never use the machine).

A turn records its runner when queued (``SetupTurn.runner``); the machine
tools refuse once the thread's runner is no longer that one
(``turn_runner_refusal``). Every turn acquisition locks the runner row
first (``lock_runner``; lock order runner, then project, then run) and asks
``runner_hold_refusal``.
"""

from __future__ import annotations

from typing import Final

from machine_setup.exceptions import (
    MachineSetupError,
    SetupMachineNotReadyError,
    SetupRunActiveError,
)
from machine_setup.models import SetupConversation, SetupConversationKind
from projects.models import CaseHelpRequest, TestRunTestCase
from projects.runner_use import (
    RunnerHoldKind,
    agent_run_started_on_runner,
    runner_hold,
)
from runners.locks import lock_runner_row
from runners.machine_state import (
    DEVICE_NOT_READY_MESSAGE,
    MachineState,
    device_offline,
    runner_machine_state,
)
from runners.models import TestRunner
from runners.services.runner_access import (
    USABLE_RUNNER_FIELDS,
    runner_serves_project,
    runner_serves_project_id,
)
from runners.types import RunnerId
from visual_engine.services.pool_status import get_visual_engine_status

NO_RUNNER_SELECTED_MESSAGE: Final = (
    "No Test Runner selected. Pick one in the chat header, then try again."
)
NO_RUN_RUNNER_MESSAGE: Final = (
    "This run has no Test Runner. Assign one on the run page, then try again."
)
RUNNER_CHANGED_MESSAGE: Final = (
    "The Test Runner of this chat changed after this message was sent. Send a "
    "new message to work on the new one."
)
RUNNER_CHANGED_RETRY_MESSAGE: Final = (
    "The Test Runner of this chat changed meanwhile. Send your message again."
)
RUNNER_UNAVAILABLE_MESSAGE: Final = (
    "The chat's Test Runner is no longer available to this project. Pick "
    "another one in the chat header."
)
RUN_RUNNER_UNAVAILABLE_MESSAGE: Final = (
    "This run's Test Runner is no longer available to this project. Choose "
    "another one on the run page, then try again."
)
# The tool loop's ``ToolContext`` needs a runner id even in a turn without
# one; the setup tools refuse every machine call before it is used.
NO_RUNNER: Final = RunnerId(0)

# What ``runner_machine_state`` needs from a locked or loaded runner row.
RUNNER_STATE_FIELDS: Final = USABLE_RUNNER_FIELDS

# Holds of the thread's own project that the per-project rules already
# settle: a paused agent run (help, result and auto-fix chats work on it; a
# Started one is refused separately) and a claimed Manual case (the manual
# discussion talks while the tester works; ``run_setup_task`` refuses at call
# time while the case is not paused).
_SAME_PROJECT_HOLDS: Final = frozenset(
    {RunnerHoldKind.AGENT_RUN, RunnerHoldKind.MANUAL_CASE}
)


def thread_runner_id(conversation: SetupConversation) -> RunnerId | None:
    """The thread's runner now; at most one query (none for the main chat
    and case analysis threads)."""
    kind = conversation.kind
    if kind == SetupConversationKind.RUN_TRIAGE:
        return None
    if kind == SetupConversationKind.CHAT:
        return _typed(conversation.runner_id)
    raw: int | None = None
    if conversation.help_request_id is not None:
        raw = (
            CaseHelpRequest.objects.filter(pk=conversation.help_request_id)
            .values_list("test_run_test_case__test_run__runner_id", flat=True)
            .first()
        )
    else:
        pivot_id = conversation.result_case_id or conversation.manual_case_id
        if pivot_id is not None:
            raw = (
                TestRunTestCase.objects.filter(pk=pivot_id)
                .values_list("test_run__runner_id", flat=True)
                .first()
            )
    return _typed(raw)


def main_chat_runner_id(project_id: int) -> RunnerId | None:
    """The runner picked for the project's main chat (one query); None
    without a main chat or a pick."""
    raw = (
        SetupConversation.objects.filter(
            project_id=project_id,
            kind=SetupConversationKind.CHAT,
            archived_at__isnull=True,
        )
        .values_list("runner_id", flat=True)
        .first()
    )
    return _typed(raw)


def no_runner_message(kind: str) -> str:
    """Why a thread of ``kind`` has no runner, for its machine tools."""
    if kind == SetupConversationKind.CHAT:
        return NO_RUNNER_SELECTED_MESSAGE
    return NO_RUN_RUNNER_MESSAGE


def lock_runner(runner_id: RunnerId | None) -> TestRunner | None:
    """The runner row under ``select_for_update`` (call in a transaction,
    before the project row); None for no runner or a deleted one."""
    return lock_runner_row(runner_id, fields=RUNNER_STATE_FIELDS)


def load_runner(runner_id: RunnerId | None) -> TestRunner | None:
    """The runner row (no lock) with the fields the badge needs; one query."""
    if runner_id is None:
        return None
    return TestRunner.objects.only(*RUNNER_STATE_FIELDS).filter(pk=runner_id).first()


def runner_usable_by_project(runner: TestRunner, project_id: int) -> bool:
    """A main chat may keep using a runner while it serves the project
    (``runner_access.runner_serves_project``; one query)."""
    return runner_serves_project(runner, project_id)


def runner_hold_refusal(
    runner_id: RunnerId, project_id: int, *, ignore_turn_id: int | None = None
) -> MachineSetupError | None:
    """Why a new turn of ``project_id`` may not drive the runner now.

    A Started agent run on the runner refuses (``SetupRunActiveError``); any
    other holder (another project's run or claim, a setup turn, a
    self-update, a readiness scan or check) refuses with its neutral message.
    A paused agent run or a claimed Manual case of the same project is
    accepted (``_SAME_PROJECT_HOLDS``) and the later holders still count.
    Call it under the runner row lock. A handful of cheap queries.
    """
    if agent_run_started_on_runner(runner_id):
        return SetupRunActiveError()
    hold = runner_hold(
        runner_id,
        ignore_turn_id=ignore_turn_id,
        allow=lambda held: held.project_id == project_id
        and held.kind in _SAME_PROJECT_HOLDS,
    )
    return None if hold is None else MachineSetupError(hold.message)


def machine_not_ready_refusal(
    runner: TestRunner | None, kind: str
) -> MachineSetupError | None:
    """The READY gate of help, result and auto-fix threads: the runner
    connected, the visual engine ready and, on a phone runner, the device
    READY (no query besides the engine status)."""
    if runner is None:
        return SetupMachineNotReadyError(no_runner_message(kind))
    if runner_machine_state(runner, get_visual_engine_status()) == MachineState.READY:
        return None
    if device_offline(runner):
        return SetupMachineNotReadyError(DEVICE_NOT_READY_MESSAGE)
    return SetupMachineNotReadyError()


def turn_runner_refusal(
    conversation_id: int, kind: str, turn_runner_id: RunnerId | None
) -> str | None:
    """Why the turn may not use the machine: no runner recorded, the
    thread's runner changed since the turn was queued, or the runner no
    longer serves the project (archived, or its owner left the project or
    was deactivated: ``runner_serves_project``). Read now, at call time:
    three queries at most."""
    if turn_runner_id is None:
        return no_runner_message(kind)
    conversation = (
        SetupConversation.objects.only(
            "id",
            "project_id",
            "kind",
            "runner_id",
            "help_request_id",
            "result_case_id",
            "manual_case_id",
        )
        .filter(pk=conversation_id)
        .first()
    )
    if conversation is None or thread_runner_id(conversation) != turn_runner_id:
        return RUNNER_CHANGED_MESSAGE
    if not runner_serves_project_id(turn_runner_id, conversation.project_id):
        if conversation.kind == SetupConversationKind.CHAT:
            return RUNNER_UNAVAILABLE_MESSAGE
        return RUN_RUNNER_UNAVAILABLE_MESSAGE
    return None


def _typed(raw: int | None) -> RunnerId | None:
    return None if raw is None else RunnerId(raw)
