"""The Test Runner preselected on a run's Start form (plan Q13).

The run's own runner when the viewer may pick it; else the user's runner
last used for this project, if it is free; else the only free, Ready runner
of the user; else none. "Free": connected, its phone (if any) ready, and no
holder (``runner_use.runner_holds``, one bulk check for all candidates).
Only runners the user may pick for the project count
(``runner_access.load_usable_runners``).
"""

from __future__ import annotations

from collections.abc import Sequence

from accounts.models import CustomUser
from projects.models import Project, TestRun
from projects.runner_use import runner_holds
from runners.machine_state import MachineState, device_offline, runner_machine_state
from runners.models import TestRunner
from runners.services.runner_access import load_usable_runners
from runners.types import RunnerId
from visual_engine.services.pool_status import get_visual_engine_status
from visual_engine.types import VisualEngineStatus


def start_runner_initial(
    user: CustomUser,
    project: Project,
    test_run: TestRun,
    *,
    usable: Sequence[TestRunner] | None = None,
    visual_engine: VisualEngineStatus | None = None,
) -> RunnerId | None:
    """The Start form's preselected runner for ``test_run``: the run's own
    when it is among the viewer's usable runners, else the Q13 default.

    ``usable`` / ``visual_engine``: the rows and engine status the caller
    already loaded (the run page's runner card), so they are not read twice.
    """
    if usable is None:
        usable = load_usable_runners(user, project)
    if test_run.runner_id is not None and any(
        runner.pk == test_run.runner_id for runner in usable
    ):
        return RunnerId(test_run.runner_id)
    return default_runner_for_start(
        user, project, usable=usable, visual_engine=visual_engine
    )


def default_runner_for_start(
    user: CustomUser,
    project: Project,
    *,
    usable: Sequence[TestRunner] | None = None,
    visual_engine: VisualEngineStatus | None = None,
) -> RunnerId | None:
    """The runner to preselect for ``user`` starting a run of ``project``.

    The candidates (two queries, none when ``usable`` is given), the last
    used one (one query) and one bulk hold check of the connected ones (four
    queries and one cache read, whatever their number).
    """
    runners = list(usable) if usable is not None else load_usable_runners(user, project)
    if not runners:
        return None
    by_id = {runner.pk: runner for runner in runners}
    connected = [
        runner.runner_id
        for runner in runners
        if runner.connected and not device_offline(runner)
    ]
    holds = runner_holds(connected)
    free_ids = {runner_id for runner_id in connected if runner_id not in holds}
    last_used_id = (
        TestRun.objects.filter(project_id=project.pk, runner_id__in=by_id)
        .exclude(runner_assigned_at__isnull=True)
        .order_by("-runner_assigned_at", "-pk")
        .values_list("runner_id", flat=True)
        .first()
    )
    if last_used_id is not None and last_used_id in free_ids:
        return RunnerId(last_used_id)
    engine = visual_engine if visual_engine is not None else get_visual_engine_status()
    ready = [
        runner
        for runner in runners
        if runner.pk in free_ids
        and runner_machine_state(runner, engine) == MachineState.READY
    ]
    if len(ready) == 1:
        return ready[0].runner_id
    return None
