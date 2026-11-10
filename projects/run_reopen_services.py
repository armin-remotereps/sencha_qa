"""Put a finished run back to work: shared by Resume and Re-run in place.

Both lock the run's Test Runner (``run_runner_gate.lock_run_runner``), then
the project and then the run (the order `start_test_run` uses), apply the
same gates, and restart the run with a new execution generation so workers
of an earlier chain stand down.

The machine gate is the run's own Test Runner (``TestRun.runner``): it must
be set (agent modes), connected, ready and not held by another holder
(``runner_use``). The per-project rules (one active run, one live setup
turn) stay in Phase 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE, live_turn_exists
from projects.checked_push_limits import is_checked_push_live
from projects.exceptions import RunHelpError
from projects.manual_run_policy import is_manual_run
from projects.models import (
    ACTIVE_RUN_STATUSES,
    Project,
    RunHelpMode,
    TestRailPushStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_case_order import RUN_CASE_ORDERING
from projects.run_runner_assignment import RUNNER_NOT_SERVING_MESSAGE
from projects.run_runner_gate import (
    CHOOSE_RUNNER_MESSAGE,
    RUNNER_REMOVED_MESSAGE,
    ErrorFactory,
    check_run_runner_unchanged,
    lock_run_runner,
)
from projects.runner_use import runner_hold, runner_id_of_run
from projects.services import (
    broadcast_run_and_pivots,
    broadcast_test_run_status,
    finish_test_run,
    queue_test_run_cases,
)
from runners.machine_state import (
    DEVICE_NOT_READY_MESSAGE,
    MachineState,
    device_offline,
    runner_machine_state,
)
from runners.models import TestRunner
from runners.services.runner_access import runner_serves_project
from runners.types import RunnerId
from visual_engine.services.pool_status import get_visual_engine_status

REOPENABLE_RUN_STATUSES: tuple[str, ...] = (TestRunStatus.DONE, TestRunStatus.CANCELLED)

_OTHER_RUN_ACTIVE = "Another test run is already in progress for this project."
_CHECKED_PUSH_LIVE = "Wait for the checked results to finish sending to TestRail."
_RESULTS_PUSHING = "Wait for the results to finish pushing to TestRail."
_MACHINE_NOT_READY = "Connect the Test Runner and wait for the visual engine."
_RUNNER_REMOVED = RUNNER_REMOVED_MESSAGE
_MACHINE_FAILED = (
    "Every connected vision worker failed to load. "
    "Check the vision workers on the Environment page."
)
_VISUAL_ENGINE_OFFLINE = "Start a vision worker."
_NOT_PAUSED = "This run is not waiting for help."
_STILL_NEEDS_HELP = "A case of this run still needs help."
_WAITING_FOR_PERSON_STEP = "A case of this run is waiting for a person's step."

# A case's Checked/Sent marks; cleared whenever its result is replaced, so a
# new TestRail result can be sent.
CHECKED_SENT_FIELDS: tuple[str, ...] = (
    "checked_at",
    "checked_by",
    "testrail_source_result_id",
    "testrail_source_sent_at",
    "testrail_source_attachment_count",
    "testrail_source_skip_reason",
)

# Every field `reset_case_for_restart` writes, for ``save`` / ``bulk_update``.
RESTART_RESET_FIELDS: tuple[str, ...] = (
    "status",
    "blocked_reason",
    "result",
    "finished_at",
    *CHECKED_SENT_FIELDS,
    "updated_at",
)


def lock_project_and_run(
    test_run_id: int, *, project_id: int | None = None
) -> tuple[Project, TestRun]:
    """Lock project then run; the caller must be inside ``transaction.atomic``.

    ``project_id``: the run's project when the caller already read it.
    """
    if project_id is None:
        project_id = _run_project_id(test_run_id)
    project = Project.objects.select_for_update().get(pk=project_id)
    test_run = TestRun.objects.select_for_update().get(pk=test_run_id)
    return project, test_run


def _run_project_id(test_run_id: int) -> int:
    project_id: int = TestRun.objects.values_list("project_id", flat=True).get(
        pk=test_run_id
    )
    return project_id


def lock_runner_project_and_run(
    test_run_id: int,
    runner_id: RunnerId | None,
    *,
    error: ErrorFactory,
    usable: bool = True,
) -> tuple[Project, TestRun]:
    """Lock the run's runner (``run_runner_gate.lock_run_runner``), then the
    project and the run, and check the run still has that runner.

    ``runner_id`` comes from ``run_runner_gate.select_run_runner`` (None: a
    Manual run without one). Must be called inside ``transaction.atomic``.
    """
    project_id = _run_project_id(test_run_id)
    lock_run_runner(
        runner_id,
        test_run_id=test_run_id,
        project_id=project_id,
        error=error,
        usable=usable,
    )
    project, test_run = lock_project_and_run(test_run_id, project_id=project_id)
    check_run_runner_unchanged(test_run, runner_id, error=error)
    return project, test_run


def lock_run_with_its_runner(
    test_run_id: int, *, error: ErrorFactory
) -> tuple[Project, TestRun]:
    """Lock the run's current runner row (if any), then project and run.

    For the wake paths that queue a paused run's cases without choosing a
    runner (help resolutions, person-step answers, "Continue run", a mode
    switch's hand-back, the end of an auto-fix): the row lock serializes
    them against other holders of the runner, and ``list_wake_blockers``
    checks it under the lock. Must be called inside ``transaction.atomic``.
    """
    current = (
        TestRun.objects.filter(pk=test_run_id)
        .values_list("runner_id", flat=True)
        .first()
    )
    runner_id = None if current is None else RunnerId(current)
    return lock_runner_project_and_run(
        test_run_id, runner_id, error=error, usable=False
    )


def list_reopen_blockers(
    test_run: TestRun, project: Project, *, include_runner: bool = True
) -> list[str]:
    """User-safe reasons the run cannot be put back to work (run status aside).

    `list_wake_blockers` plus `list_testrail_push_blockers`: Resume, Re-run and
    the review redesign re-run replace verdicts that a live send may be
    writing. Each caller checks the run status itself, with its own message.

    ``include_runner=False`` (page panels only): leave out the run's Test
    Runner gate. The forms offer a runner choice and the action switches
    runner before it re-checks every gate under the locks, so a busy or
    missing runner must not disable the button; the run page's Test Runner
    card shows the runner's state.
    """
    return _list_blockers(
        test_run, project, include_push=True, include_runner=include_runner
    )


def list_wake_blockers(
    test_run: TestRun,
    project: Project,
    *,
    auto_fix_turn_id: int | None = None,
    include_runner: bool = True,
) -> list[str]:
    """User-safe reasons a PAUSED run cannot be woken (run status aside).

    The reopen gates without the TestRail send/push gates. Invariant: a wake
    path (`queue_pending_cases`, the help and person step panels and their
    actions) only touches NEED_HELP, AWAITING_PERSON and CREATED cases, never a
    checked case or one a send selects, so a live early send cannot conflict
    with it. A path that changes a finished verdict must use
    `list_reopen_blockers` instead.

    ``auto_fix_turn_id``: the live auto-fix turn of the run's open help
    request (``turn_state.live_auto_fix_turn_id``); resolving the request
    stops it, so that turn (on the project and on the run's runner) does not
    count. Any other setup turn on the runner still does.
    """
    return _list_blockers(
        test_run,
        project,
        include_push=False,
        auto_fix_turn_id=auto_fix_turn_id,
        include_runner=include_runner,
    )


def _list_blockers(
    test_run: TestRun,
    project: Project,
    *,
    include_push: bool,
    auto_fix_turn_id: int | None = None,
    include_runner: bool = True,
) -> list[str]:
    blockers: list[str] = []
    if _has_other_started_run(project.pk, test_run.pk):
        blockers.append(_OTHER_RUN_ACTIVE)
    if include_push:
        blockers.extend(list_testrail_push_blockers(test_run))
    # A manual run needs no machine: its tester may work anywhere (Q12).
    if include_runner and not is_manual_run(test_run):
        machine_blocker = run_runner_blocker(test_run, ignore_turn_id=auto_fix_turn_id)
        if machine_blocker:
            blockers.append(machine_blocker)
    # Phase 1 keeps one live setup turn per project.
    if auto_fix_turn_id is None and live_turn_exists(project.pk, timezone.now()):
        blockers.append(MACHINE_SETUP_RUNNING_MESSAGE)
    return blockers


def list_testrail_push_blockers(test_run: TestRun) -> list[str]:
    """User-safe reasons a TestRail send or push of the run is live (no query).

    A case's result must not change under a live Send checked or results push.
    """
    blockers: list[str] = []
    if is_checked_push_live(test_run, timezone.now()):
        blockers.append(_CHECKED_PUSH_LIVE)
    if test_run.testrail_push_status == TestRailPushStatus.PUSHING:
        blockers.append(_RESULTS_PUSHING)
    return blockers


def clear_checked_and_sent(pivot: TestRunTestCase) -> None:
    """Clear the case's Checked/Sent marks in memory (`CHECKED_SENT_FIELDS`)."""
    pivot.checked_at = None
    pivot.checked_by = None
    pivot.testrail_source_result_id = None
    pivot.testrail_source_sent_at = None
    pivot.testrail_source_attachment_count = 0
    pivot.testrail_source_skip_reason = ""


def reset_case_for_restart(pivot: TestRunTestCase, now: datetime) -> None:
    """Put a case back to CREATED in memory, as Resume does (`RESTART_RESET_FIELDS`).

    Clears the verdict, finish time and Checked/Sent marks; the log,
    screenshots and checkpoint are kept. ``started_at`` (the first start) and
    ``active_duration`` (time of the earlier segments) are kept too: the worker
    opens a new segment when it picks the case up
    (``projects.case_duration_services.open_case_segment``).
    """
    pivot.status = TestRunTestCaseStatus.CREATED
    pivot.blocked_reason = ""
    pivot.result = ""
    pivot.finished_at = None
    clear_checked_and_sent(pivot)
    # bulk_update does not run auto_now.
    pivot.updated_at = now


def is_valid_help_mode(help_mode: str) -> bool:
    return help_mode in RunHelpMode.values


def reopen_run(
    test_run: TestRun,
    pivots: Sequence[TestRunTestCase],
    *,
    help_mode: str | None = None,
) -> None:
    """Restart the locked run and queue ``pivots`` (in run order) after commit.

    The pivots must already be saved as CREATED and carry those values in
    memory, since they are broadcast as they are. ``help_mode`` (a
    ``RunHelpMode`` value) changes the run's mode; ``None`` keeps it. Callers
    validate it with `is_valid_help_mode` before any write.
    """
    update_fields = ["status", "finished_at", "execution_generation", "updated_at"]
    if help_mode is not None:
        if not is_valid_help_mode(help_mode):
            raise ValueError(f"Unknown help mode {help_mode!r}")
        test_run.help_mode = help_mode
        update_fields.append("help_mode")
    test_run.status = TestRunStatus.STARTED
    test_run.finished_at = None
    # Workers of the earlier chain see the new generation and stand down.
    test_run.execution_generation += 1
    test_run.save(update_fields=update_fields)

    if is_manual_run(test_run):
        # No execution chain: the cases wait for the tester at the Test
        # Runner window, which the broadcast nudges.
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, pivots))
        return

    pivot_ids = [pivot.pk for pivot in pivots]

    def _enqueue() -> None:
        if queue_test_run_cases(test_run, pivot_ids):
            broadcast_run_and_pivots(test_run, pivots)

    # Workers must see the STARTED run and CREATED cases.
    transaction.on_commit(_enqueue)


def queue_pending_cases(test_run: TestRun) -> None:
    """Wake a PAUSED run: queue its CREATED cases in run order after commit.

    Used when a Need help case is resolved: the helped case is CREATED and
    earlier than the rest, so it runs first. The caller holds the project and
    run locks (`lock_project_and_run`). With no CREATED case left the run is
    finished (DONE) instead, without the gates.

    Raises ``RunHelpError`` (user-safe) when the run is not PAUSED, a case of
    it still needs help or waits for a person's step (Human in the loop), or a
    wake gate fails (`list_wake_blockers`).
    """
    if test_run.status != TestRunStatus.PAUSED:
        raise RunHelpError(_NOT_PAUSED)
    # Logs can be large and are never touched here.
    open_pivots = list(
        TestRunTestCase.objects.filter(
            test_run_id=test_run.pk,
            status__in=[
                TestRunTestCaseStatus.CREATED,
                TestRunTestCaseStatus.NEED_HELP,
                TestRunTestCaseStatus.AWAITING_PERSON,
            ],
        )
        .defer("logs")
        .order_by(*RUN_CASE_ORDERING)
    )
    if any(pivot.status == TestRunTestCaseStatus.NEED_HELP for pivot in open_pivots):
        raise RunHelpError(_STILL_NEEDS_HELP)
    if any(
        pivot.status == TestRunTestCaseStatus.AWAITING_PERSON for pivot in open_pivots
    ):
        raise RunHelpError(_WAITING_FOR_PERSON_STEP)
    if not open_pivots:
        _finish_run(test_run)
        return

    blockers = list_wake_blockers(test_run, test_run.project)
    if blockers:
        raise RunHelpError(blockers[0])
    reopen_run(test_run, open_pivots)


def _finish_run(test_run: TestRun) -> None:
    """Every case of the woken PAUSED run is settled: the run is DONE.

    Goes through `finish_test_run` (conditional on PAUSED and the run's
    current generation), which also schedules the run-finished notification.
    """
    if finish_test_run(
        test_run,
        from_status=TestRunStatus.PAUSED,
        generation=test_run.execution_generation,
    ):
        transaction.on_commit(lambda: broadcast_test_run_status(test_run))


def _has_other_started_run(project_id: int, test_run_id: int) -> bool:
    """Another run of the project is Started or Paused (the Phase 1 rule:
    one active run per project, whatever its runner)."""
    return (
        TestRun.objects.filter(project_id=project_id, status__in=ACTIVE_RUN_STATUSES)
        .exclude(pk=test_run_id)
        .exists()
    )


def run_runner_blocker(
    test_run: TestRun, *, ignore_turn_id: int | None = None
) -> str | None:
    """Why the run's Test Runner cannot serve it now, or None (user-safe).

    No runner (an agent run must choose one), removed, no longer serving the
    project (its owner left or was deactivated), not connected, its
    phone not ready, the visual engine not ready, or held by another holder
    (``runner_use.runner_hold``; the run itself and ``ignore_turn_id``, a
    live auto-fix turn the action stops, do not count). One runner
    query, one membership query, plus the hold checks. A form that lets the person choose another
    runner switches first (``run_runner_gate.select_run_runner``), so this
    reason no longer applies after the switch.
    """
    runner_id = runner_id_of_run(test_run)
    if runner_id is None:
        return CHOOSE_RUNNER_MESSAGE
    runner = (
        TestRunner.objects.filter(pk=runner_id, archived_at__isnull=True)
        .only(
            "id",
            "owner_id",
            "archived_at",
            "connected",
            "device_state",
            "device_reported_at",
        )
        .first()
    )
    if runner is None:
        return _RUNNER_REMOVED
    if not runner_serves_project(runner, test_run.project_id):
        return RUNNER_NOT_SERVING_MESSAGE
    machine_blocker = _machine_blocker(runner)
    if machine_blocker:
        return machine_blocker
    hold = runner_hold(
        runner_id, ignore_run_id=test_run.pk, ignore_turn_id=ignore_turn_id
    )
    return None if hold is None else hold.message


def _machine_blocker(runner: TestRunner) -> str | None:
    state = runner_machine_state(runner, get_visual_engine_status())
    if state == MachineState.READY:
        return None
    if device_offline(runner):
        # DEVICE_OFFLINE, or STARTING while the phone's session is created.
        return DEVICE_NOT_READY_MESSAGE
    if state == MachineState.FAILED:
        return _MACHINE_FAILED
    if state == MachineState.VISUAL_ENGINE_OFFLINE:
        return _VISUAL_ENGINE_OFFLINE
    return _MACHINE_NOT_READY
