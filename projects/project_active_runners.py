"""Which Test Runners a project's active runs hold (read-only).

Used by the project's "Test Runners" tab and overview, the project tables
(``/projects/``) and the superuser ``/all-projects/`` page in place of the
retired per-project machine badge (A23). One query each, whatever the number
of projects: active runs are few, so they are read with their runner and its
owner and counted in Python.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace

from projects.models import ACTIVE_RUN_STATUSES, Project, TestRun
from projects.runner_display import RunnerSummary, summarize_runner
from visual_engine.types import VisualEngineStatus

_RUN_FIELDS = (
    "id",
    "project_id",
    "name",
    "status",
    "help_mode",
    "created_at",
    "runner_id",
    "runner_name_snapshot",
    "runner__name",
    "runner__archived_at",
    "runner__connected",
    "runner__system_info",
    "runner__device_state",
    "runner__device_reported_at",
    "runner__owner__email",
    "runner__owner__first_name",
    "runner__owner__last_name",
)


@dataclass(frozen=True)
class ActiveRunRunner:
    """One active run of the project and the runner it holds."""

    test_run_id: int
    run_name: str
    run_status: str
    help_mode: str
    # None: the run has no runner (a Manual run, or one carried over).
    runner: RunnerSummary | None


@dataclass(frozen=True)
class ProjectActiveRuns:
    """A project row's runner column."""

    active_run_count: int
    # The runner of the latest active run that has one, else None.
    latest_runner: RunnerSummary | None


def _active_runs(project_ids: Collection[int]) -> list[TestRun]:
    return list(
        TestRun.objects.filter(
            project_id__in=project_ids, status__in=ACTIVE_RUN_STATUSES
        )
        .select_related("runner__owner")
        .only(*_RUN_FIELDS)
        .order_by("project_id", "-created_at", "-id")
    )


def _summary(
    run: TestRun, visual_engine: VisualEngineStatus, *, show_hosts: bool = True
) -> RunnerSummary | None:
    runner = run.runner
    if runner is None or runner.archived_at is not None:
        return None
    summary = summarize_runner(runner, visual_engine)
    return summary if show_hosts else replace(summary, hostname="")


def list_active_run_runners(
    project: Project, visual_engine: VisualEngineStatus, *, show_hosts: bool
) -> list[ActiveRunRunner]:
    """The project's active runs, newest first, with their runners.

    ``show_hosts`` False (a viewer who cannot write) blanks the machines'
    hostnames.
    """
    return [
        ActiveRunRunner(
            test_run_id=run.id,
            run_name=run.display_name,
            run_status=run.status,
            help_mode=run.help_mode,
            runner=_summary(run, visual_engine, show_hosts=show_hosts),
        )
        for run in _active_runs([project.pk])
    ]


def summarize_active_runs(items: Sequence[ActiveRunRunner]) -> ProjectActiveRuns:
    """The project row form of ``list_active_run_runners`` (no query)."""
    return ProjectActiveRuns(
        active_run_count=len(items),
        latest_runner=next(
            (item.runner for item in items if item.runner is not None), None
        ),
    )


def active_runs_by_project(
    project_ids: Collection[int], visual_engine: VisualEngineStatus
) -> dict[int, ProjectActiveRuns]:
    """Per project id with an active run: its count and the latest runner.

    Projects without an active run are missing from the result.
    """
    if not project_ids:
        return {}
    counts: dict[int, int] = {}
    latest: dict[int, RunnerSummary] = {}
    for run in _active_runs(project_ids):
        counts[run.project_id] = counts.get(run.project_id, 0) + 1
        if run.project_id not in latest:
            summary = _summary(run, visual_engine)
            if summary is not None:
                latest[run.project_id] = summary
    return {
        project_id: ProjectActiveRuns(
            active_run_count=count, latest_runner=latest.get(project_id)
        )
        for project_id, count in counts.items()
    }
