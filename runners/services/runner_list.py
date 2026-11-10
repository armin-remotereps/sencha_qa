"""Data of the ``/runners/`` list pages (the owner's list and ``/runners/all/``).

A fixed number of queries whatever the number of runners: one for the
runners, one for the active runs assigned to them, one for the live setup
turns driving them and one for the viewer's memberships of those projects
(none for a superuser). The visual engine status is read once.

The hold shown here is the cheap, list-level view of
``projects.runner_use.runner_hold``: an active agent run, else a live setup
turn. The detail page shows the full hold (claims, update, scans).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from django.db.models import Q, QuerySet
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.models import LIVE_TURN_STATUSES, SetupTurn
from machine_setup.turn_state import SETUP_TURN_STALE_AFTER
from projects.models import (
    ACTIVE_RUN_STATUSES,
    ProjectMembership,
    RunHelpMode,
    TestRun,
)
from projects.project_access import has_superuser_project_control

# Re-exported for ``runner_detail`` until it imports the leaf directly.
from runners.device_label import runner_device_label
from runners.machine_state import runner_machine_state
from runners.models import TestRunner
from runners.services.runner_access import owned_runners
from visual_engine.services.pool_status import get_visual_engine_status


class ListHoldKind(StrEnum):
    AGENT_RUN = "agent_run"
    SETUP_TURN = "setup_turn"


@dataclass(frozen=True)
class ListHold:
    """What keeps the runner busy. ``project_id`` / ``test_run_id`` are set
    only when the viewer may open that project (then the page links it and
    names it); otherwise ``label`` is neutral."""

    kind: ListHoldKind
    label: str
    project_id: int | None
    test_run_id: int | None


@dataclass(frozen=True)
class RunnerListRow:
    runner: TestRunner
    # A ``runners.machine_state.MachineState`` value (seeds the live badge).
    machine_state: str
    os_label: str
    # "" for a desktop runner.
    device_label: str
    hold: ListHold | None
    # Active runs (STARTED or PAUSED, Manual included) assigned to it.
    active_run_count: int


@dataclass(frozen=True)
class _RunRow:
    run_id: int
    runner_id: int
    project_id: int
    project_name: str
    run_name: str
    manual: bool


@dataclass(frozen=True)
class _TurnRow:
    runner_id: int
    project_id: int
    project_name: str


def os_label(system_info: object) -> str:
    """ "macOS 15.1" from the handshake ``system_info``; "" when unknown."""
    if not isinstance(system_info, Mapping):
        return ""
    os_name = str(system_info.get("os") or "")
    version = str(system_info.get("os_version") or "")
    return f"{os_name} {version}".strip() if os_name else ""


def run_label(run_name: str, run_id: int) -> str:
    return run_name or f"Run #{run_id}"


def list_owner_runners(user: CustomUser) -> list[RunnerListRow]:
    """The user's non-archived runners with state and hold (four queries)."""
    return _rows(list(owned_runners(user)), viewer=user)


def list_all_runners(viewer: CustomUser) -> list[RunnerListRow]:
    """Every non-archived runner with its owner (superuser page, read-only)."""
    runners: QuerySet[TestRunner] = (
        TestRunner.objects.filter(archived_at__isnull=True)
        .select_related("owner")
        .order_by("owner__email", "name", "pk")
    )
    return _rows(list(runners), viewer=viewer)


def _rows(runners: list[TestRunner], *, viewer: CustomUser) -> list[RunnerListRow]:
    if not runners:
        return []
    runner_ids = [runner.pk for runner in runners]
    runs = _active_runs(runner_ids)
    turns = _live_turns(runner_ids)
    visible = _visible_project_ids(
        viewer, {row.project_id for row in runs} | {row.project_id for row in turns}
    )
    runs_by_runner: dict[int, list[_RunRow]] = {}
    for run in runs:
        runs_by_runner.setdefault(run.runner_id, []).append(run)
    turn_by_runner = {turn.runner_id: turn for turn in turns}
    visual_engine = get_visual_engine_status()
    rows: list[RunnerListRow] = []
    for runner in runners:
        assigned = runs_by_runner.get(runner.pk, [])
        rows.append(
            RunnerListRow(
                runner=runner,
                machine_state=runner_machine_state(runner, visual_engine).value,
                os_label=os_label(runner.system_info),
                device_label=runner_device_label(runner),
                hold=_hold(assigned, turn_by_runner.get(runner.pk), visible),
                active_run_count=len(assigned),
            )
        )
    return rows


def _active_runs(runner_ids: list[int]) -> list[_RunRow]:
    rows = (
        TestRun.objects.filter(runner_id__in=runner_ids, status__in=ACTIVE_RUN_STATUSES)
        .order_by("pk")
        .values_list(
            "pk", "runner_id", "project_id", "project__name", "name", "help_mode"
        )
    )
    return [
        _RunRow(
            run_id=pk,
            runner_id=runner_id,
            project_id=project_id,
            project_name=project_name,
            run_name=name,
            manual=help_mode == RunHelpMode.MANUAL,
        )
        for pk, runner_id, project_id, project_name, name, help_mode in rows
    ]


def _live_turns(runner_ids: list[int]) -> list[_TurnRow]:
    """Queued or running, non-stale turns (same rule as
    ``machine_setup.turn_state.runner_turn_live``)."""
    cutoff = timezone.now() - SETUP_TURN_STALE_AFTER
    rows = (
        SetupTurn.objects.filter(
            Q(started_at__gte=cutoff)
            | Q(started_at__isnull=True, created_at__gte=cutoff),
            runner_id__in=runner_ids,
            status__in=LIVE_TURN_STATUSES,
        )
        .order_by("pk")
        .values_list("runner_id", "project_id", "project__name")
    )
    return [
        _TurnRow(runner_id=runner_id, project_id=project_id, project_name=name)
        for runner_id, project_id, name in rows
        if runner_id is not None
    ]


def _visible_project_ids(viewer: CustomUser, project_ids: Iterable[int]) -> set[int]:
    wanted = set(project_ids)
    if not wanted:
        return set()
    if has_superuser_project_control(viewer):
        return wanted
    return set(
        ProjectMembership.objects.filter(
            user_id=viewer.pk, project_id__in=wanted, project__archived=False
        ).values_list("project_id", flat=True)
    )


def _hold(
    assigned: list[_RunRow], turn: _TurnRow | None, visible: set[int]
) -> ListHold | None:
    agent_run = next((run for run in assigned if not run.manual), None)
    if agent_run is not None:
        if agent_run.project_id in visible:
            return ListHold(
                kind=ListHoldKind.AGENT_RUN,
                label=(
                    f"{run_label(agent_run.run_name, agent_run.run_id)} · "
                    f"{agent_run.project_name}"
                ),
                project_id=agent_run.project_id,
                test_run_id=agent_run.run_id,
            )
        return ListHold(ListHoldKind.AGENT_RUN, "A run of another project", None, None)
    if turn is not None:
        if turn.project_id in visible:
            return ListHold(
                kind=ListHoldKind.SETUP_TURN,
                label=f"Machine setup · {turn.project_name}",
                project_id=turn.project_id,
                test_run_id=None,
            )
        return ListHold(ListHoldKind.SETUP_TURN, "Machine setup", None, None)
    return None
