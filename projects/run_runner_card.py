"""The run page's "Test Runner" card, and the runner choices of the run's
forms (Start, Resume, Re-run, help Continue, person-step report).

Read-only: the writes are ``projects.run_runner_assignment`` (Change) and
``projects.run_runner_release`` (Release). Three queries at most: the run's
runner with its owner, and the viewer's usable runners.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from accounts.models import CustomUser
from projects.models import Project, RunHelpMode, TestRun
from projects.run_runner_assignment import (
    ASSIGNABLE_RUN_STATUSES,
    REASSIGNABLE_RUN_STATUSES,
)
from projects.runner_choices import RunnerChoice, runner_choices
from projects.runner_display import RunnerSummary, summarize_runner
from runners.models import TestRunner
from runners.services.runner_access import load_usable_runners
from visual_engine.types import VisualEngineStatus


@dataclass(frozen=True)
class RunRunnerCard:
    # The run's runner; None when the run has none (or it was archived).
    runner: RunnerSummary | None
    # The name the run kept when its runner is gone ("" otherwise).
    snapshot_name: str
    # The connected runner's handshake system info ({} without a runner).
    system_info: dict[str, Any]
    # The viewer owns the run's runner (Release).
    is_owner: bool
    # The run's status allows choosing a runner now (Q6) and the viewer has
    # runners to choose from; the template also requires write access.
    can_change: bool
    # The viewer's own runners: the choices of every runner select.
    choices: tuple[RunnerChoice, ...]
    # A runner must be chosen before the run goes on: an agent run without
    # one (Manual runs may have none, Q12).
    runner_required: bool
    # The viewer's usable runner rows behind ``choices``
    # (``runner_access.USABLE_RUNNER_FIELDS``), shared with the Start form's
    # default and the readiness checklist so they are loaded once.
    usable_runners: tuple[TestRunner, ...] = ()


def get_run_runner_card(
    test_run: TestRun,
    project: Project,
    user: CustomUser,
    visual_engine: VisualEngineStatus,
    *,
    can_write: bool,
) -> RunRunnerCard:
    """``project`` is the run's project (already loaded by the view).

    ``can_write``: the viewer may write in the project; a read-only viewer
    gets no hostname of the run's machine.
    """
    runner: TestRunner | None = None
    if test_run.runner_id is not None:
        runner = (
            TestRunner.objects.filter(pk=test_run.runner_id, archived_at__isnull=True)
            .select_related("owner")
            .first()
        )
    usable = tuple(load_usable_runners(user, project))
    choices = tuple(runner_choices(user, project, runners=usable))
    status_allows_change = (
        test_run.status in ASSIGNABLE_RUN_STATUSES
        or test_run.status in REASSIGNABLE_RUN_STATUSES
    )
    system_info = (
        runner.system_info
        if runner is not None and isinstance(runner.system_info, dict)
        else {}
    )
    summary = None if runner is None else summarize_runner(runner, visual_engine)
    if summary is not None and not can_write:
        summary = replace(summary, hostname="")
    return RunRunnerCard(
        runner=summary,
        snapshot_name="" if runner is not None else test_run.runner_name_snapshot,
        system_info=system_info,
        is_owner=runner is not None and runner.owner_id == user.pk,
        can_change=status_allows_change and bool(choices),
        choices=choices,
        runner_required=runner is None and test_run.help_mode != RunHelpMode.MANUAL,
        usable_runners=usable,
    )
