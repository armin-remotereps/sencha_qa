"""Data of the runner detail page (``/runners/<id>/``, owner only).

Queries: the runner, ``runner_hold`` (stops at the first hold), the active
runs assigned to it, the owner's memberships of their projects, and the
update offer.
"""

from __future__ import annotations

from dataclasses import dataclass

from accounts.models import CustomUser
from projects.models import ACTIVE_RUN_STATUSES, ProjectMembership, RunHelpMode, TestRun
from projects.runner_update_offer import RunnerUpdateOffer, get_runner_update_offer
from projects.runner_use import RunnerHold, runner_hold
from runners.device_label import runner_device_label
from runners.exceptions import RunnerNotFoundError
from runners.models import TestRunner
from runners.services.runner_access import can_manage
from runners.services.runner_list import os_label
from runners.services.runner_status import RunnerStatusEvent, build_runner_status_event
from runners.types import RunnerId
from visual_engine.services.pool_status import get_visual_engine_status


@dataclass(frozen=True)
class AssignedRun:
    test_run_id: int
    project_id: int
    project_name: str
    label: str
    status_label: str
    manual: bool
    # The owner may open the project (still a member); else no link and no names.
    visible: bool


@dataclass(frozen=True)
class RunnerDetail:
    runner: TestRunner
    # ``RunnerStatusEvent`` seeding the live page (same shape as the socket).
    status: RunnerStatusEvent
    os_label: str
    device_label: str
    hold: RunnerHold | None
    # The holder's project is one the owner may open.
    hold_visible: bool
    assigned_runs: list[AssignedRun]
    update_offer: RunnerUpdateOffer


def get_owned_runner(*, actor: CustomUser, runner_id: RunnerId) -> TestRunner:
    """The actor's non-archived runner; ``RunnerNotFoundError`` otherwise
    (also for another user's runner, so its existence is not revealed)."""
    runner = TestRunner.objects.filter(pk=runner_id, archived_at__isnull=True).first()
    if runner is None or not can_manage(actor, runner):
        raise RunnerNotFoundError()
    return runner


def get_runner_detail(*, actor: CustomUser, runner_id: RunnerId) -> RunnerDetail:
    runner = get_owned_runner(actor=actor, runner_id=runner_id)
    runs = list(
        TestRun.objects.filter(runner_id=runner.pk, status__in=ACTIVE_RUN_STATUSES)
        .select_related("project")
        .only("pk", "name", "status", "help_mode", "project__id", "project__name")
        .order_by("pk")
    )
    hold = runner_hold(runner.runner_id)
    project_ids = {run.project_id for run in runs}
    if hold is not None and hold.project_id is not None:
        project_ids.add(hold.project_id)
    visible = (
        set(
            ProjectMembership.objects.filter(
                user_id=actor.pk, project_id__in=project_ids, project__archived=False
            ).values_list("project_id", flat=True)
        )
        if project_ids
        else set()
    )
    return RunnerDetail(
        runner=runner,
        status=build_runner_status_event(runner, get_visual_engine_status()),
        os_label=os_label(runner.system_info),
        device_label=runner_device_label(runner),
        hold=hold,
        hold_visible=hold is not None and hold.project_id in visible,
        assigned_runs=[
            AssignedRun(
                test_run_id=run.pk,
                project_id=run.project_id,
                project_name=run.project.name,
                label=run.display_name,
                status_label=run.get_status_display(),
                manual=run.help_mode == RunHelpMode.MANUAL,
                visible=run.project_id in visible,
            )
            for run in runs
        ],
        update_offer=get_runner_update_offer(runner),
    )
