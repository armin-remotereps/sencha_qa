"""Put a finished run back to work: shared by Resume and Re-run in place.

Both lock the project and then the run (the order `start_test_run` uses),
apply the same gates, and restart the run with a new execution generation so
workers of an earlier chain stand down.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.db import transaction
from django.utils import timezone

from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE, live_turn_exists
from projects.checked_push_limits import is_checked_push_live
from projects.models import (
    Project,
    TestRailPushStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
)
from projects.services import (
    MachineState,
    broadcast_run_and_pivots,
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
    if is_checked_push_live(test_run, timezone.now()):
        blockers.append(_CHECKED_PUSH_LIVE)
    if test_run.testrail_push_status == TestRailPushStatus.PUSHING:
        blockers.append(_RESULTS_PUSHING)
    machine_blocker = _machine_blocker(project)
    if machine_blocker:
        blockers.append(machine_blocker)
    if live_turn_exists(project.pk, timezone.now()):
        blockers.append(MACHINE_SETUP_RUNNING_MESSAGE)
    return blockers


def reopen_run(test_run: TestRun, pivots: Sequence[TestRunTestCase]) -> None:
    """Restart the locked run and queue ``pivots`` (in run order) after commit.

    The pivots must already be saved as CREATED and carry those values in
    memory, since they are broadcast as they are.
    """
    test_run.status = TestRunStatus.STARTED
    test_run.finished_at = None
    # Workers of the earlier chain see the new generation and stand down.
    test_run.execution_generation += 1
    test_run.save(
        update_fields=[
            "status",
            "finished_at",
            "execution_generation",
            "updated_at",
        ]
    )

    pivot_ids = [pivot.pk for pivot in pivots]

    def _enqueue() -> None:
        if queue_test_run_cases(test_run, pivot_ids):
            broadcast_run_and_pivots(test_run, pivots)

    # Workers must see the STARTED run and CREATED cases.
    transaction.on_commit(_enqueue)


def _has_other_started_run(project_id: int, test_run_id: int) -> bool:
    return (
        TestRun.objects.filter(project_id=project_id, status=TestRunStatus.STARTED)
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
