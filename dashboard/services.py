from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Count, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from projects.models import (
    Project,
    TestCase,
    TestCaseUpload,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
    UploadStatus,
)
from projects.services import (
    MachineState,
    PrimaryAction,
    get_machine_state,
    resolve_primary_action,
)

RECENT_PROJECTS_LIMIT = 3
ATTENTION_LIMIT = 5
ACTIVITY_LIMIT = 8


@dataclass(frozen=True)
class RunSummary:
    """The dashboard's four headline stat cards."""

    runs_in_progress: int
    passed_7d: int
    failed_7d: int
    pass_rate_7d: int | None
    never_run_cases: int


@dataclass(frozen=True)
class AttentionItem:
    """One row in the "Needs your attention" list."""

    kind: str
    project: Project
    title: str
    detail: str
    occurred_at: datetime
    url: str


@dataclass(frozen=True)
class ProjectCard:
    """One "Recent projects" card, including its one suggested next action."""

    project: Project
    machine_state: MachineState
    case_count: int
    latest_run: TestRun | None
    latest_run_summary: dict[str, int] | None
    primary_action: PrimaryAction
    primary_action_url: str
    primary_action_label: str


@dataclass(frozen=True)
class ActivityItem:
    """One row in the "Recent activity" feed."""

    kind: str
    project: Project
    title: str
    occurred_at: datetime
    failed: bool
    url: str


@dataclass(frozen=True)
class DashboardData:
    """Everything the dashboard index page needs to render."""

    has_projects: bool
    summary: RunSummary
    attention: list[AttentionItem]
    projects: list[ProjectCard]
    activity: list[ActivityItem]


def _empty_dashboard_data() -> DashboardData:
    return DashboardData(
        has_projects=False,
        summary=RunSummary(
            runs_in_progress=0,
            passed_7d=0,
            failed_7d=0,
            pass_rate_7d=None,
            never_run_cases=0,
        ),
        attention=[],
        projects=[],
        activity=[],
    )


def _build_run_summary(project_ids: list[int], since: datetime) -> RunSummary:
    runs_in_progress = TestRun.objects.filter(
        project_id__in=project_ids, status=TestRunStatus.STARTED
    ).count()

    finished_counts = TestRunTestCase.objects.filter(
        test_run__project_id__in=project_ids,
        finished_at__gte=since,
        status__in=[TestRunTestCaseStatus.SUCCESS, TestRunTestCaseStatus.FAILED],
    ).aggregate(
        passed=Count("id", filter=Q(status=TestRunTestCaseStatus.SUCCESS)),
        failed=Count("id", filter=Q(status=TestRunTestCaseStatus.FAILED)),
    )
    passed = finished_counts["passed"] or 0
    failed = finished_counts["failed"] or 0
    total = passed + failed
    pass_rate = round(passed * 100 / total) if total else None

    never_run_cases = TestCase.objects.filter(
        project_id__in=project_ids, run_entries__isnull=True
    ).count()

    return RunSummary(
        runs_in_progress=runs_in_progress,
        passed_7d=passed,
        failed_7d=failed,
        pass_rate_7d=pass_rate,
        never_run_cases=never_run_cases,
    )


def _failed_run_attention_items(
    project_ids: list[int], since: datetime
) -> list[AttentionItem]:
    runs = (
        TestRun.objects.filter(
            project_id__in=project_ids,
            status=TestRunStatus.DONE,
            finished_at__gte=since,
        )
        .annotate(
            failed_count=Count(
                "pivot_entries",
                filter=Q(pivot_entries__status=TestRunTestCaseStatus.FAILED),
            )
        )
        .filter(failed_count__gt=0)
        .select_related("project")
        .order_by("-finished_at")[:ATTENTION_LIMIT]
    )
    return [_failed_run_attention_item(run) for run in runs]


def _failed_run_attention_item(run: TestRun) -> AttentionItem:
    failed_count: int = run.failed_count  # type: ignore[attr-defined]  # annotate() field; django-stubs does not model dynamic query annotations
    return AttentionItem(
        kind="failed_run",
        project=run.project,
        title=f"{run.display_name} had failures",
        detail=f"{failed_count} case(s) failed in {run.project.name}.",
        occurred_at=run.finished_at or run.updated_at,
        url=reverse("projects:test_run_detail", args=[run.project_id, run.id]),
    )


def _disconnected_machine_attention_items(
    project_ids: list[int],
) -> list[AttentionItem]:
    projects = (
        Project.objects.filter(
            id__in=project_ids,
            agent_connected=False,
            test_runs__status=TestRunStatus.WAITING,
        )
        .distinct()
        .order_by("-updated_at")[:ATTENTION_LIMIT]
    )
    return [
        AttentionItem(
            kind="disconnected_machine",
            project=project,
            title=f"{project.name}'s test machine is disconnected",
            detail="This project has a draft run waiting for a connected machine.",
            occurred_at=project.last_connected_at or project.updated_at,
            url=reverse("projects:environment", args=[project.id]),
        )
        for project in projects
    ]


def _primary_action_label(
    action: PrimaryAction,
    project: Project,
    latest_run_summary: dict[str, int] | None,
) -> str:
    if action == PrimaryAction.IMPORT_CASES:
        return "Import test cases"
    if action == PrimaryAction.CONNECT_MACHINE:
        if project.last_connected_at is not None:
            return "Reconnect test machine"
        return "Connect test machine"
    if action == PrimaryAction.CREATE_RUN:
        return "Run tests"
    if action == PrimaryAction.VIEW_ACTIVE_RUN:
        return "View running tests"

    had_failures = bool(latest_run_summary and latest_run_summary.get("failed"))
    return "Review failed run" if had_failures else "Review latest run"


def _primary_action_url(
    action: PrimaryAction,
    project: Project,
    active_run: TestRun | None,
    latest_run: TestRun | None,
) -> str:
    if action == PrimaryAction.IMPORT_CASES:
        return reverse("projects:test_case_import", args=[project.id])
    if action == PrimaryAction.CONNECT_MACHINE:
        return reverse("projects:environment", args=[project.id])
    if action == PrimaryAction.CREATE_RUN:
        return reverse("projects:test_case_list", args=[project.id])
    if action == PrimaryAction.VIEW_ACTIVE_RUN and active_run is not None:
        return reverse("projects:test_run_detail", args=[project.id, active_run.id])
    if latest_run is not None:
        return reverse("projects:test_run_detail", args=[project.id, latest_run.id])
    return reverse("projects:detail", args=[project.id])


def _summaries_for_runs(run_ids: set[int]) -> dict[int, dict[str, int]]:
    if not run_ids:
        return {}
    rows = (
        TestRunTestCase.objects.filter(test_run_id__in=run_ids)
        .values("test_run_id")
        .annotate(
            success=Count("id", filter=Q(status=TestRunTestCaseStatus.SUCCESS)),
            failed=Count("id", filter=Q(status=TestRunTestCaseStatus.FAILED)),
        )
    )
    return {
        row["test_run_id"]: {"success": row["success"], "failed": row["failed"]}
        for row in rows
    }


def _build_project_card(
    project: Project,
    runs_by_id: dict[int, TestRun],
    summaries_by_run_id: dict[int, dict[str, int]],
) -> ProjectCard:
    # The three attributes below are annotate() fields on the project row.
    case_count: int = project.case_count  # type: ignore[attr-defined]
    active_run = runs_by_id.get(project.active_run_id)  # type: ignore[attr-defined]
    latest_run = runs_by_id.get(project.latest_run_id)  # type: ignore[attr-defined]
    machine_state = get_machine_state(project)
    latest_run_summary = (
        summaries_by_run_id.get(latest_run.id) if latest_run is not None else None
    )
    primary_action = resolve_primary_action(
        case_count=case_count,
        machine_state=machine_state,
        active_run=active_run,
        latest_run=latest_run,
    )
    return ProjectCard(
        project=project,
        machine_state=machine_state,
        case_count=case_count,
        latest_run=latest_run,
        latest_run_summary=latest_run_summary,
        primary_action=primary_action,
        primary_action_url=_primary_action_url(
            primary_action, project, active_run, latest_run
        ),
        primary_action_label=_primary_action_label(
            primary_action, project, latest_run_summary
        ),
    )


def _build_project_cards(project_ids: list[int]) -> list[ProjectCard]:
    """Build the "Recent projects" cards with a fixed number of queries.

    Latest/active run ids are resolved via correlated subqueries on the
    projects query itself, then the run rows and their pass/fail summaries
    are fetched in two batched queries instead of per-project.
    """
    latest_run_qs = TestRun.objects.filter(project=OuterRef("pk")).order_by(
        "-created_at"
    )
    active_run_qs = TestRun.objects.filter(
        project=OuterRef("pk"), status=TestRunStatus.STARTED
    )
    projects = list(
        Project.objects.filter(id__in=project_ids)
        .annotate(
            case_count=Count("test_cases", distinct=True),
            latest_run_id=Subquery(latest_run_qs.values("pk")[:1]),
            active_run_id=Subquery(active_run_qs.values("pk")[:1]),
        )
        .order_by("-updated_at")[:RECENT_PROJECTS_LIMIT]
    )

    run_ids: set[int] = {
        run_id
        for project in projects
        for run_id in (
            project.latest_run_id,
            project.active_run_id,
        )
        if run_id is not None
    }
    runs_by_id = TestRun.objects.in_bulk(run_ids)
    summaries_by_run_id = _summaries_for_runs(run_ids)

    return [
        _build_project_card(project, runs_by_id, summaries_by_run_id)
        for project in projects
    ]


def _upload_activity_item(upload: TestCaseUpload) -> ActivityItem:
    return ActivityItem(
        kind="import_completed",
        project=upload.project,
        title=(
            f"Imported {upload.processed_cases} test case(s) from "
            f"{upload.original_filename}"
        ),
        occurred_at=upload.updated_at,
        failed=False,
        url=reverse("projects:test_case_import", args=[upload.project_id]),
    )


def _run_activity_item(run: TestRun) -> ActivityItem:
    failed_count: int = run.failed_count  # type: ignore[attr-defined]  # annotate() field; django-stubs does not model dynamic query annotations
    if run.finished_at is not None:
        return ActivityItem(
            kind="run_finished",
            project=run.project,
            title=f"{run.display_name} finished",
            occurred_at=run.finished_at,
            failed=failed_count > 0,
            url=reverse("projects:test_run_detail", args=[run.project_id, run.id]),
        )
    occurred_at = run.started_at or run.created_at
    return ActivityItem(
        kind="run_started",
        project=run.project,
        title=f"{run.display_name} started",
        occurred_at=occurred_at,
        failed=False,
        url=reverse("projects:test_run_detail", args=[run.project_id, run.id]),
    )


def _build_activity_items(project_ids: list[int]) -> list[ActivityItem]:
    uploads = list(
        TestCaseUpload.objects.filter(
            project_id__in=project_ids, status=UploadStatus.COMPLETED
        )
        .select_related("project")
        .order_by("-updated_at")[:ACTIVITY_LIMIT]
    )
    runs = list(
        TestRun.objects.filter(project_id__in=project_ids)
        .exclude(started_at__isnull=True)
        .annotate(
            failed_count=Count(
                "pivot_entries",
                filter=Q(pivot_entries__status=TestRunTestCaseStatus.FAILED),
            )
        )
        .select_related("project")
        .order_by("-updated_at")[:ACTIVITY_LIMIT]
    )

    items = [_upload_activity_item(upload) for upload in uploads] + [
        _run_activity_item(run) for run in runs
    ]
    items.sort(key=lambda item: item.occurred_at, reverse=True)
    return items[:ACTIVITY_LIMIT]


def get_dashboard_data(
    user: CustomUser, *, now: datetime | None = None, window_days: int = 7
) -> DashboardData:
    """Gather everything the dashboard index page needs.

    Runs a constant, small number of queries regardless of how many
    projects the user belongs to: one to resolve the project id scope,
    then batched aggregates/subqueries per section instead of one query
    per project or per run.
    """
    current_time = now or timezone.now()
    since = current_time - timedelta(days=window_days)

    project_ids = list(
        Project.objects.filter(members=user, archived=False).values_list(
            "id", flat=True
        )
    )
    if not project_ids:
        return _empty_dashboard_data()

    summary = _build_run_summary(project_ids, since)
    attention = _failed_run_attention_items(
        project_ids, since
    ) + _disconnected_machine_attention_items(project_ids)
    projects = _build_project_cards(project_ids)
    activity = _build_activity_items(project_ids)

    return DashboardData(
        has_projects=True,
        summary=summary,
        attention=attention,
        projects=projects,
        activity=activity,
    )
