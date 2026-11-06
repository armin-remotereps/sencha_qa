"""Superuser use case: list every project in the installation.

The way in for a superuser to projects they are not a member of. Membership
scoped pages (dashboard, failures, ``/projects/``) stay unchanged.

Every row value is a correlated subquery or ``Exists`` (never a join that fans
out), so a page costs a constant number of queries whatever its size:
paginator count, page rows, tags prefetch and latest runs (4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core.paginator import Page, Paginator
from django.db.models import (
    CharField,
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
)
from django.db.models.functions import Coalesce, Lower

from accounts.exceptions import SuperuserRequiredError
from accounts.models import CustomUser
from dashboard.all_projects_types import (
    ALL_PROJECTS_QUERY_MAX_LENGTH,
    AllProjectsFilters,
    AllProjectsSort,
    MembershipFilter,
    ProjectStatusFilter,
    RunActivityFilter,
    RunnerFilter,
)
from projects.models import (
    ACTIVE_RUN_STATUSES,
    WRITABLE_ROLES,
    Project,
    ProjectMembership,
    ProjectRole,
    TestCase,
    TestRun,
)
from projects.project_access import has_superuser_project_control
from projects.runner_target import device_machine_state
from projects.services import get_latest_runs_by_project


@dataclass(frozen=True)
class AllProjectsLatestRun:
    id: int
    label: str
    status: str
    created_at: datetime
    failed_count: int
    blocked_count: int
    retest_count: int


@dataclass(frozen=True)
class AllProjectsRow:
    id: int
    name: str
    archived: bool
    created_at: datetime
    tag_names: tuple[str, ...]
    case_count: int
    member_count: int
    writable_member_count: int
    # "Member" / "Read-only" / "Not a member": the actor's own membership.
    viewer_role_label: str
    viewer_is_member: bool
    runner_connected: bool
    runner_version: str
    # "" unless a phone runner's device holds the badge: "starting" or
    # "device_offline" (``MachineState`` values).
    device_machine_state: str
    last_connected_at: datetime | None
    latest_run: AllProjectsLatestRun | None


_NOT_A_MEMBER_LABEL = "Not a member"


def _count(qs: QuerySet[TestCase] | QuerySet[ProjectMembership]) -> Coalesce:
    """``COUNT(*)`` of a queryset correlated on ``project=OuterRef("pk")``."""
    counted = qs.order_by().values("project").annotate(n=Count("pk")).values("n")
    return Coalesce(Subquery(counted, output_field=IntegerField()), 0)


def _filtered(actor: CustomUser, filters: AllProjectsFilters) -> QuerySet[Project]:
    qs = Project.objects.all()
    if filters.status is ProjectStatusFilter.ACTIVE:
        qs = qs.filter(archived=False)
    elif filters.status is ProjectStatusFilter.ARCHIVED:
        qs = qs.filter(archived=True)

    if filters.runner is RunnerFilter.CONNECTED:
        qs = qs.filter(agent_connected=True)
    elif filters.runner is RunnerFilter.DISCONNECTED:
        qs = qs.filter(agent_connected=False)

    if filters.run is not RunActivityFilter.ANY:
        has_active_run = Exists(
            TestRun.objects.filter(
                project=OuterRef("pk"), status__in=ACTIVE_RUN_STATUSES
            )
        )
        qs = qs.filter(
            has_active_run
            if filters.run is RunActivityFilter.ACTIVE
            else ~has_active_run
        )

    if filters.membership is not MembershipFilter.ANY:
        is_member = Exists(
            ProjectMembership.objects.filter(project=OuterRef("pk"), user=actor)
        )
        qs = qs.filter(
            is_member if filters.membership is MembershipFilter.MINE else ~is_member
        )

    query = filters.query.strip()[:ALL_PROJECTS_QUERY_MAX_LENGTH]
    if query:
        # Exists, not joins: no duplicate rows, so no distinct().
        member_email_matches = Exists(
            ProjectMembership.objects.filter(
                project=OuterRef("pk"), user__email__icontains=query
            )
        )
        tag_matches = Exists(
            Project.tags.through.objects.filter(
                project_id=OuterRef("pk"), tag__name__icontains=query
            )
        )
        qs = qs.filter(Q(name__icontains=query) | member_email_matches | tag_matches)
    return qs


def list_all_projects(
    *,
    actor: CustomUser,
    filters: AllProjectsFilters,
    page: int,
    per_page: int,
) -> Page[AllProjectsRow]:
    """One page of every project matching ``filters``.

    Raises ``SuperuserRequiredError`` unless ``actor`` controls every project
    (``has_superuser_project_control``); the view's decorator checks it first.
    """
    if not has_superuser_project_control(actor):
        raise SuperuserRequiredError()

    latest_runs = TestRun.objects.filter(project=OuterRef("pk")).order_by(
        "-created_at", "-id"
    )
    qs = (
        _filtered(actor, filters)
        .only(
            "id",
            "name",
            "archived",
            "created_at",
            "agent_connected",
            "agent_client_version",
            "agent_device_state",
            "agent_device_reported_at",
            "last_connected_at",
        )
        .annotate(
            case_count=_count(TestCase.objects.filter(project=OuterRef("pk"))),
            member_count=_count(
                ProjectMembership.objects.filter(
                    project=OuterRef("pk"), user__is_active=True
                )
            ),
            writable_member_count=_count(
                ProjectMembership.objects.filter(
                    project=OuterRef("pk"),
                    role__in=WRITABLE_ROLES,
                    user__is_active=True,
                )
            ),
            viewer_role=Subquery(
                ProjectMembership.objects.filter(
                    project=OuterRef("pk"), user=actor
                ).values("role")[:1],
                output_field=CharField(),
            ),
            latest_run_id=Subquery(latest_runs.values("pk")[:1]),
        )
        .prefetch_related("tags")
    )
    if filters.sort is AllProjectsSort.NAME:
        qs = qs.order_by(Lower("name"), "id")
    elif filters.sort is AllProjectsSort.ACTIVITY:
        qs = qs.annotate(
            latest_run_at=Subquery(latest_runs.values("created_at")[:1])
        ).order_by(F("latest_run_at").desc(nulls_last=True), "-id")
    else:
        qs = qs.order_by("-created_at", "-id")

    paginator: Paginator[Project] = Paginator(qs, per_page)
    project_page = paginator.get_page(page)
    runs_by_project = get_latest_runs_by_project(project_page.object_list)
    rows = [
        _to_row(project, runs_by_project.get(project.id))
        for project in project_page.object_list
    ]
    # The paginator counted the projects; this page holds the same rows mapped
    # to AllProjectsRow, which django-stubs' Page[T] cannot express.
    return Page(rows, project_page.number, paginator)  # type: ignore[arg-type]


def _role_label(role: str | None) -> str:
    if not role:
        return _NOT_A_MEMBER_LABEL
    return str(ProjectRole(role).label) if role in ProjectRole.values else role


def _to_row(project: Project, run: TestRun | None) -> AllProjectsRow:
    # The annotate() fields below are not modelled by django-stubs.
    return AllProjectsRow(
        id=project.id,
        name=project.name,
        archived=project.archived,
        created_at=project.created_at,
        tag_names=tuple(sorted(tag.name for tag in project.tags.all())),
        case_count=project.case_count,  # type: ignore[attr-defined]  # annotate() field
        member_count=project.member_count,  # type: ignore[attr-defined]  # annotate() field
        writable_member_count=project.writable_member_count,  # type: ignore[attr-defined]  # annotate() field
        viewer_role_label=_role_label(project.viewer_role),  # type: ignore[attr-defined]  # annotate() field
        viewer_is_member=bool(project.viewer_role),  # type: ignore[attr-defined]  # annotate() field
        runner_connected=project.agent_connected,
        runner_version=project.agent_client_version,
        device_machine_state=device_machine_state(project) or "",
        last_connected_at=project.last_connected_at,
        latest_run=None if run is None else _to_latest_run(run),
    )


def _to_latest_run(run: TestRun) -> AllProjectsLatestRun:
    return AllProjectsLatestRun(
        id=run.id,
        label=run.display_name,
        status=run.status,
        created_at=run.created_at,
        failed_count=run.failed_count,  # type: ignore[attr-defined]  # annotate() field
        blocked_count=run.blocked_count,  # type: ignore[attr-defined]  # annotate() field
        retest_count=run.retest_count,  # type: ignore[attr-defined]  # annotate() field
    )
