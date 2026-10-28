"""Put a finished run back to work: shared by Resume and Re-run in place.

Both lock the project and then the run (the order `start_test_run` uses),
apply the same gates, and restart the run with a new execution generation so
workers of an earlier chain stand down.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE, live_turn_exists
from projects.checked_push_limits import is_checked_push_live
from projects.exceptions import RunHelpError
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
from projects.services import (
    MachineState,
    broadcast_run_and_pivots,
    broadcast_test_run_status,
    get_machine_state,
    queue_test_run_cases,
)
from visual_engine.services.pool_status import get_visual_engine_status

REOPENABLE_RUN_STATUSES: tuple[str, ...] = (TestRunStatus.DONE, TestRunStatus.CANCELLED)

_OTHER_RUN_ACTIVE = "Another test run is already in progress for this project."
_CHECKED_PUSH_LIVE = "Wait for the checked results to finish sending to TestRail."
_RESULTS_PUSHING = "Wait for the results to finish pushing to TestRail."
_MACHINE_NOT_READY = "Connect the Test Runner and wait for the visual engine."
_MACHINE_FAILED = (
    "Every connected vision worker failed to load. "
    "Check the vision workers on the Environment page."
)
_VISUAL_ENGINE_OFFLINE = "Start a vision worker."
_NOT_PAUSED = "This run is not waiting for help."
_STILL_NEEDS_HELP = "A case of this run still needs help."

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


def lock_project_and_run(test_run_id: int) -> tuple[Project, TestRun]:
    """Lock project then run; the caller must be inside ``transaction.atomic``."""
    project_id = TestRun.objects.values_list("project_id", flat=True).get(
        pk=test_run_id
    )
    project = Project.objects.select_for_update().get(pk=project_id)
    test_run = TestRun.objects.select_for_update().get(pk=test_run_id)
    return project, test_run


def list_reopen_blockers(test_run: TestRun, project: Project) -> list[str]:
    """User-safe reasons the run cannot be put back to work (run status aside).

    Each caller checks the run status itself, with its own message.
    """
    blockers: list[str] = []
    if _has_other_started_run(project.pk, test_run.pk):
        blockers.append(_OTHER_RUN_ACTIVE)
    blockers.extend(list_testrail_push_blockers(test_run))
    machine_blocker = _machine_blocker(project)
    if machine_blocker:
        blockers.append(machine_blocker)
    if live_turn_exists(project.pk, timezone.now()):
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
    it still needs help, or a reopen gate fails (`list_reopen_blockers`).
    """
    if test_run.status != TestRunStatus.PAUSED:
        raise RunHelpError(_NOT_PAUSED)
    # Logs can be large and are never touched here.
    open_pivots = list(
        TestRunTestCase.objects.filter(
            test_run_id=test_run.pk,
            status__in=[TestRunTestCaseStatus.CREATED, TestRunTestCaseStatus.NEED_HELP],
        )
        .defer("logs")
        .order_by(*RUN_CASE_ORDERING)
    )
    if any(pivot.status == TestRunTestCaseStatus.NEED_HELP for pivot in open_pivots):
        raise RunHelpError(_STILL_NEEDS_HELP)
    if not open_pivots:
        _finish_run(test_run)
        return

    blockers = list_reopen_blockers(test_run, test_run.project)
    if blockers:
        raise RunHelpError(blockers[0])
    reopen_run(test_run, open_pivots)


def _finish_run(test_run: TestRun) -> None:
    """Every case of the woken run is settled: the run is DONE."""
    test_run.status = TestRunStatus.DONE
    test_run.finished_at = timezone.now()
    test_run.save(update_fields=["status", "finished_at", "updated_at"])
    transaction.on_commit(lambda: broadcast_test_run_status(test_run))


def _has_other_started_run(project_id: int, test_run_id: int) -> bool:
    """Another run is Started or Paused: it holds the project's machine."""
    return (
        TestRun.objects.filter(project_id=project_id, status__in=ACTIVE_RUN_STATUSES)
        .exclude(pk=test_run_id)
        .exists()
    )


def _machine_blocker(project: Project) -> str | None:
    state = get_machine_state(project, get_visual_engine_status())
    if state == MachineState.READY:
        return None
    if state == MachineState.FAILED:
        return _MACHINE_FAILED
    if state == MachineState.VISUAL_ENGINE_OFFLINE:
        return _VISUAL_ENGINE_OFFLINE
    return _MACHINE_NOT_READY
