from __future__ import annotations

import asyncio
import base64
import io
import logging
import secrets
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, NoReturn, Protocol, TypedDict, TypeVar

from asgiref.sync import async_to_sync
from celery import chain
from celery.exceptions import SoftTimeLimitExceeded
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.paginator import Page, Paginator
from django.db import transaction
from django.db.models import (
    Case,
    Count,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Concat
from django.urls import reverse
from django.utils import dateformat, timezone

from accounts.models import CustomUser
from agents.checkpoint import read_checkpoint_help
from agents.types import (
    AgentCancelledError,
    AgentResult,
    AgentStopReason,
    CancellationCheck,
    HelpRequestInfo,
    LogCallback,
    OrchestratorCheckpoint,
    PixelParseResult,
    ScreenshotCallback,
)
from auto_tester.celery import app as celery_app
from controller_client.protocol import (
    CLIENT_VERSION,
    REQUIRED_CLIENT_CAPABILITIES,
    BrowserName,
    ClientCapability,
    ErrorCode,
)
from machine_setup.auto_fix_stop import request_auto_fix_stop
from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE, live_turn_exists
from projects.application_context_services import write_application_context
from projects.case_checkpoint_services import (
    build_checkpoint_callback,
    load_resume_checkpoint,
)
from projects.case_failed_notifications import notify_case_failed
from projects.case_faults import (
    classify_case_fault,
    fault_ask,
    fault_message,
    help_continue_mode,
)
from projects.case_help_notifications import notify_case_needs_help
from projects.case_help_policy import auto_fix_eligible
from projects.case_help_state import (
    RunHelpState,
    enqueue_case_auto_fix,
    get_run_help_state,
    mark_auto_fix_running,
)
from projects.case_step_progress import (
    StepProgressPayload,
    case_step_payload,
    pivot_step_payload,
)
from projects.case_teardown_policy import needs_teardown
from projects.checked_push_limits import is_checked_push_live
from projects.controller_outage_clock import agent_monotonic, reset_outage_clock
from projects.controller_reconnect_services import (
    connected_owner,
    controller_connection_lost,
    wait_for_controller,
)
from projects.exceptions import (
    BrowserUnavailableError,
    ControllerActionError,
    ControllerNotConnectedError,
    ControllerReconnectTimeoutError,
    TestRunnerDisconnectedError,
    TestRunnerUnresponsiveError,
)
from projects.models import (
    ACTIVE_RUN_STATUSES,
    HELP_REQUEST_TEXT_MAX_LENGTH,
    ApplicationPlatform,
    BlockedReason,
    CaseHelpRequest,
    CasePlanRevision,
    ContextWriteSource,
    HelpAutoFixState,
    HelpContinueMode,
    HelpProposalAction,
    HelpResolution,
    PlanRevisionStatus,
    Project,
    RunHelpMode,
    Tag,
    TestCase,
    TestCaseData,
    TestCaseUpload,
    TestRailPushStatus,
    TestRun,
    TestRunCaseResume,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
    UploadStatus,
)
from projects.run_case_order import PICKER_CASE_ORDERING, RUN_CASE_ORDERING
from visual_engine.services.parse_screenshot import parse_screenshot
from visual_engine.services.pool_status import (
    get_visual_engine_status,
    list_ready_workers,
)
from visual_engine.services.wait_for_ready_worker import wait_for_ready_worker
from visual_engine.types import VisualEngineState, VisualEngineStatus

logger = logging.getLogger(__name__)


class _CaseTeardown(Protocol):
    """``agents.services.case_teardown.run_case_teardown``, injected (import cycle)."""

    def __call__(
        self,
        checkpoint: OrchestratorCheckpoint,
        project_id: int,
        *,
        timeout_seconds: int,
        on_log: LogCallback | None,
        cancellation_check: CancellationCheck | None,
        system_info: dict[str, object] | None,
        project_prompt: str | None,
        machine_ready: Callable[[], bool],
    ) -> None: ...


_INTERACTIVE_TERMINATE_TIMEOUT_SECONDS = 30.0


@transaction.atomic
def create_project(*, user: CustomUser, name: str, tag_names: list[str]) -> Project:
    project = Project.objects.create(name=name)
    _sync_tags(project, tag_names)
    project.members.add(user)
    return project


@transaction.atomic
def update_project(*, project: Project, name: str, tag_names: list[str]) -> Project:
    project.name = name
    project.save()
    _sync_tags(project, tag_names)
    return project


def archive_project(project: Project) -> None:
    project.archived = True
    project.save()


def unarchive_project(project: Project) -> None:
    project.archived = False
    project.save(update_fields=["archived", "updated_at"])


def _clone_test_case(*, source: TestCase, target_project: Project) -> TestCase:
    return TestCase(
        project=target_project,
        upload=None,
        testrail_id=source.testrail_id,
        title=source.title,
        template=source.template,
        type=source.type,
        priority=source.priority,
        estimate=source.estimate,
        references=source.references,
        preconditions=source.preconditions,
        steps=source.steps,
        expected=source.expected,
        is_converted=source.is_converted,
        testrail_suite_id=source.testrail_suite_id,
        testrail_position=source.testrail_position,
    )


@transaction.atomic
def duplicate_project(
    *, source_project: Project, user: CustomUser, name: str
) -> Project:
    """Copies tags and test cases (without upload association) from the source."""
    new_project = Project.objects.create(name=name)
    _sync_tags(new_project, list(source_project.tags.values_list("name", flat=True)))
    new_project.members.add(user)
    source_cases = TestCase.objects.filter(project=source_project)
    copies = [
        _clone_test_case(source=tc, target_project=new_project) for tc in source_cases
    ]
    TestCase.objects.bulk_create(copies)
    return new_project


@transaction.atomic
def copy_test_cases_to_project(
    *, source_project: Project, target_project: Project, test_case_ids: list[int]
) -> int:
    """Only test cases belonging to source_project are copied. Returns the count."""
    source_cases = TestCase.objects.filter(id__in=test_case_ids, project=source_project)
    copies = [
        _clone_test_case(source=tc, target_project=target_project)
        for tc in source_cases
    ]
    TestCase.objects.bulk_create(copies)
    return len(copies)


def list_other_projects_for_user(
    *, user: CustomUser, exclude_project: Project
) -> QuerySet[Project]:
    """Results are ordered alphabetically by name."""
    return (
        Project.objects.filter(members=user, archived=False)
        .exclude(id=exclude_project.id)
        .order_by("name")
    )


def get_project_for_user(project_id: int, user: CustomUser) -> Project | None:
    try:
        return Project.objects.filter(id=project_id, archived=False, members=user).get()
    except Project.DoesNotExist:
        return None


def get_project_by_id(project_id: int, user: CustomUser) -> Project | None:
    try:
        return Project.objects.filter(id=project_id, members=user).get()
    except Project.DoesNotExist:
        return None


def get_project_tag_names(project: Project) -> list[str]:
    return list(project.tags.order_by("name").values_list("name", flat=True))


def count_test_cases(project: Project) -> int:
    return project.test_cases.count()


RUN_CASE_PICKER_LIMIT = 200


@dataclass(frozen=True)
class RunCasePicker:
    """Cases offered by the wizard's run-picker plus the project's full count."""

    test_cases: list[TestCase]
    total_count: int


def get_run_case_picker(
    project: Project, *, limit: int = RUN_CASE_PICKER_LIMIT
) -> RunCasePicker:
    cases = project.test_cases.order_by(*PICKER_CASE_ORDERING)
    return RunCasePicker(test_cases=list(cases[:limit]), total_count=cases.count())


def list_projects_for_user(
    *,
    user: CustomUser,
    search: str | None,
    tag_filter: str | None,
    page: int,
    per_page: int,
    include_archived: bool = False,
) -> Page[Project]:
    latest_run_qs = TestRun.objects.filter(project=OuterRef("pk")).order_by(
        "-created_at"
    )
    # Scope to the user's projects via an `id__in` subquery rather than a
    # direct `members=user` join: a direct join would be reused by the
    # `member_count` annotation below (both reference "members"), which
    # would silently narrow that count down to the single matching row.
    qs: QuerySet[Project] = Project.objects.filter(
        id__in=Project.objects.filter(members=user).values("id")
    ).prefetch_related("tags")
    if not include_archived:
        qs = qs.filter(archived=False)
    qs = qs.annotate(
        # `members` and `test_cases` are both joined in, so each must be
        # counted `distinct` or the join fan-out inflates both counts.
        case_count=Count("test_cases", distinct=True),
        member_count=Count("members", distinct=True),
        latest_run_id=Subquery(latest_run_qs.values("pk")[:1]),
    ).order_by("-created_at")

    if search:
        qs = qs.filter(name__icontains=search)

    if tag_filter:
        qs = qs.filter(tags__name=tag_filter)

    paginator: Paginator[Project] = Paginator(qs, per_page)
    return paginator.get_page(page)


def get_latest_runs_by_project(projects: Iterable[Project]) -> dict[int, TestRun]:
    """Fetch each project's latest run, with pass/fail/blocked counts, in one query.

    `projects` must come from a queryset annotated with `latest_run_id`
    (see `list_projects_for_user`); the result is keyed by `project_id`.
    """
    latest_run_ids = [
        run_id
        for project in projects
        if (run_id := project.latest_run_id) is not None  # type: ignore[attr-defined]  # annotate() field; django-stubs does not model dynamic query annotations
    ]
    if not latest_run_ids:
        return {}

    runs = TestRun.objects.filter(id__in=latest_run_ids).annotate(
        success_count=Count(
            "pivot_entries",
            filter=Q(pivot_entries__status=TestRunTestCaseStatus.SUCCESS),
        ),
        failed_count=Count(
            "pivot_entries",
            filter=Q(pivot_entries__status=TestRunTestCaseStatus.FAILED),
        ),
        blocked_count=Count(
            "pivot_entries",
            filter=Q(pivot_entries__status=TestRunTestCaseStatus.BLOCKED),
        ),
    )
    return {run.project_id: run for run in runs}


def get_all_tags_for_user(user: CustomUser) -> QuerySet[Tag]:
    return (
        Tag.objects.filter(projects__members=user, projects__archived=False)
        .distinct()
        .order_by("name")
    )


def _normalize_tag_names(tag_names: list[str]) -> list[str]:
    return list({name.strip().lower() for name in tag_names if name.strip()})


def _get_or_create_tags(names: list[str]) -> list[Tag]:
    tags: list[Tag] = []
    for name in names:
        tag, _ = Tag.objects.get_or_create(name=name)
        tags.append(tag)
    return tags


def _sync_tags(project: Project, tag_names: list[str]) -> None:
    normalized = _normalize_tag_names(tag_names)
    tags = _get_or_create_tags(normalized)
    project.tags.set(tags)


# ============================================================================
# PROJECT OVERVIEW SERVICES
# ============================================================================


class MachineState(StrEnum):
    """The test machine's connection/readiness state, as shown in the UI."""

    DISCONNECTED = "disconnected"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    VISUAL_ENGINE_OFFLINE = "offline"


_MACHINE_STATE_BY_ENGINE_STATE: Final[dict[VisualEngineState, MachineState]] = {
    VisualEngineState.READY: MachineState.READY,
    VisualEngineState.STARTING: MachineState.STARTING,
    VisualEngineState.FAILED: MachineState.FAILED,
    VisualEngineState.OFFLINE: MachineState.VISUAL_ENGINE_OFFLINE,
}


def connected_machine_state(visual_engine: VisualEngineStatus) -> MachineState:
    """The machine state any project with a connected Test Runner has right now.

    The visual engine is global, so every connected project shares it; pages
    listing many projects compute it once instead of once per row.
    """
    return _MACHINE_STATE_BY_ENGINE_STATE[visual_engine.state]


def get_machine_state(
    project: Project, visual_engine: VisualEngineStatus
) -> MachineState:
    """Derive one machine badge state from `agent_connected` and the visual engine.

    The engine status is passed in so pages rendering many projects read it
    once (`get_visual_engine_status`) rather than once per project.
    """
    if not project.agent_connected:
        return MachineState.DISCONNECTED
    return connected_machine_state(visual_engine)


class PrimaryAction(StrEnum):
    """The single most useful next step to suggest for a project."""

    IMPORT_CASES = "import_cases"
    CONNECT_MACHINE = "connect_machine"
    CREATE_RUN = "create_run"
    VIEW_ACTIVE_RUN = "view_active_run"
    VIEW_LATEST_RUN = "view_latest_run"


def resolve_primary_action(
    *,
    case_count: int,
    machine_state: MachineState,
    active_run: TestRun | None,
    latest_run: TestRun | None,
) -> PrimaryAction:
    """Pick a project's primary action; shared by the overview and dashboard cards."""
    if case_count == 0:
        return PrimaryAction.IMPORT_CASES
    if machine_state == MachineState.DISCONNECTED:
        return PrimaryAction.CONNECT_MACHINE
    if active_run is not None:
        return PrimaryAction.VIEW_ACTIVE_RUN
    if latest_run is not None:
        return PrimaryAction.VIEW_LATEST_RUN
    return PrimaryAction.CREATE_RUN


@dataclass(frozen=True)
class ProjectOverview:
    """Everything the project overview page needs, gathered in one call."""

    case_count: int
    never_run_count: int
    machine_state: MachineState
    active_run: TestRun | None
    latest_run: TestRun | None
    latest_run_summary: dict[str, int] | None
    draft_run_count: int
    has_application_context: bool
    primary_action: PrimaryAction
    pass_rate_30d: int | None
    recent_runs: list[TestRun]


RECENT_RUNS_LIMIT = 5


def _annotate_run_counts(qs: QuerySet[TestRun]) -> QuerySet[TestRun]:
    """Annotate a `TestRun` queryset with its case/success/failure/blocked counts.

    Shared by `list_test_runs_for_project` and `get_project_overview` so
    both surfaces report the same numbers from the same query shape.
    """
    return qs.annotate(
        case_count=Count("pivot_entries"),
        success_count=Count(
            "pivot_entries",
            filter=Q(pivot_entries__status=TestRunTestCaseStatus.SUCCESS),
        ),
        failed_count=Count(
            "pivot_entries",
            filter=Q(pivot_entries__status=TestRunTestCaseStatus.FAILED),
        ),
        blocked_count=Count(
            "pivot_entries",
            filter=Q(pivot_entries__status=TestRunTestCaseStatus.BLOCKED),
        ),
    )


def _compute_pass_rate_30d(project: Project) -> int | None:
    """Pass rate over pivots that finished in the last 30 days, or None if none did."""
    since = timezone.now() - timedelta(days=30)
    result = TestRunTestCase.objects.filter(
        test_run__project=project,
        finished_at__gte=since,
        status__in=[TestRunTestCaseStatus.SUCCESS, TestRunTestCaseStatus.FAILED],
    ).aggregate(
        total=Count("id"),
        success=Count("id", filter=Q(status=TestRunTestCaseStatus.SUCCESS)),
    )
    total = result["total"] or 0
    if total == 0:
        return None
    return round((result["success"] or 0) * 100 / total)


def get_project_overview(
    project: Project, visual_engine: VisualEngineStatus
) -> ProjectOverview:
    """Gather the project overview page's data in a fixed, small number of queries."""
    case_count = project.test_cases.count()
    never_run_count = project.test_cases.filter(run_entries__isnull=True).count()
    machine_state = get_machine_state(project, visual_engine)
    active_run = TestRun.objects.filter(
        project=project, status__in=ACTIVE_RUN_STATUSES
    ).first()
    latest_run = TestRun.objects.filter(project=project).order_by("-created_at").first()
    latest_run_summary = (
        get_test_run_summary(latest_run) if latest_run is not None else None
    )
    draft_run_count = TestRun.objects.filter(
        project=project, status=TestRunStatus.WAITING
    ).count()
    primary_action = resolve_primary_action(
        case_count=case_count,
        machine_state=machine_state,
        active_run=active_run,
        latest_run=latest_run,
    )
    recent_runs = list(
        _annotate_run_counts(TestRun.objects.filter(project=project)).order_by(
            "-created_at"
        )[:RECENT_RUNS_LIMIT]
    )
    return ProjectOverview(
        case_count=case_count,
        never_run_count=never_run_count,
        machine_state=machine_state,
        active_run=active_run,
        latest_run=latest_run,
        latest_run_summary=latest_run_summary,
        draft_run_count=draft_run_count,
        has_application_context=project.has_application_context,
        primary_action=primary_action,
        pass_rate_30d=_compute_pass_rate_30d(project),
        recent_runs=recent_runs,
    )


@dataclass(frozen=True)
class ReadinessCheck:
    """One line item in the "Ready to run?" checklist."""

    key: str
    passed: bool
    blocking: bool
    title: str
    help: str
    link_url: str | None


@dataclass(frozen=True)
class RunReadiness:
    """The full "Ready to run?" checklist for a project (and optionally a run)."""

    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def blockers(self) -> tuple[ReadinessCheck, ...]:
        return tuple(
            check for check in self.checks if check.blocking and not check.passed
        )


def _cases_selected_check(
    test_run: TestRun | None, selected_count: int | None
) -> ReadinessCheck:
    if selected_count is not None:
        count = selected_count
    elif test_run is not None:
        count = test_run.pivot_entries.count()
    else:
        count = 0
    passed = count > 0
    if passed:
        title = f"{count} test case{'s' if count != 1 else ''} selected"
        help_text = ""
    else:
        title = "No test cases selected"
        help_text = "Select at least one test case to run."
    return ReadinessCheck(
        key="cases_selected",
        passed=passed,
        blocking=True,
        title=title,
        help=help_text,
        link_url=None,
    )


def _machine_connected_check(
    project: Project, machine_state: MachineState
) -> ReadinessCheck:
    passed = machine_state != MachineState.DISCONNECTED
    if passed:
        title = "Test machine connected"
        help_text = ""
    else:
        title = "Test machine not connected"
        help_text = "Runs need a connected test machine to execute steps."
    return ReadinessCheck(
        key="machine_connected",
        passed=passed,
        blocking=True,
        title=title,
        help=help_text,
        link_url=reverse("projects:environment", args=[project.id]),
    )


_VISUAL_ENGINE_NOT_READY_HELP: Final[dict[VisualEngineState, str]] = {
    VisualEngineState.STARTING: (
        "A vision worker is loading its model — this usually takes a few "
        "minutes after it connects."
    ),
    VisualEngineState.FAILED: (
        "Every connected vision worker failed to load; check the worker's log "
        "or run `python -m vision_worker.diagnose`."
    ),
    VisualEngineState.OFFLINE: (
        "No vision worker is connected. Start one with `python -m vision_worker.main`."
    ),
}


def _visual_engine_ready_check(visual_engine: VisualEngineStatus) -> ReadinessCheck:
    passed = visual_engine.state == VisualEngineState.READY
    if passed:
        title = "Visual engine ready"
        help_text = ""
    else:
        title = "Visual engine not ready"
        help_text = _VISUAL_ENGINE_NOT_READY_HELP[visual_engine.state]
    return ReadinessCheck(
        key="visual_engine_ready",
        passed=passed,
        blocking=True,
        title=title,
        help=help_text,
        link_url=None,
    )


def _no_active_run_check(project: Project, test_run: TestRun | None) -> ReadinessCheck:
    active_runs = TestRun.objects.filter(
        project=project, status__in=ACTIVE_RUN_STATUSES
    )
    if test_run is not None:
        active_runs = active_runs.exclude(id=test_run.id)
    passed = not active_runs.exists()
    if passed:
        title = "No other run active"
        help_text = ""
    else:
        title = "Another run is in progress"
        help_text = "Only one run can execute at a time for this project."
    return ReadinessCheck(
        key="no_active_run",
        passed=passed,
        blocking=True,
        title=title,
        help=help_text,
        link_url=reverse("projects:test_run_list", args=[project.id]),
    )


def _application_context_check(project: Project) -> ReadinessCheck:
    passed = project.has_application_context
    if passed:
        title = "Application context provided"
        help_text = ""
    else:
        title = "No application context set"
        help_text = (
            "Add the application URL, platform, or notes so the AI agent "
            "knows what it is testing."
        )
    return ReadinessCheck(
        key="application_context",
        passed=passed,
        blocking=False,
        title=title,
        help=help_text,
        link_url=reverse("projects:application_context", args=[project.id]),
    )


def _no_machine_setup_check(project: Project) -> ReadinessCheck:
    passed = not live_turn_exists(project.id, timezone.now())
    if passed:
        title = "Machine setup is not running"
        help_text = ""
    else:
        title = "Machine setup is running"
        help_text = (
            "The setup agent is working on this machine. "
            "Stop it or wait for it to finish."
        )
    return ReadinessCheck(
        key="no_machine_setup",
        passed=passed,
        blocking=True,
        title=title,
        help=help_text,
        link_url=reverse("machine_setup:chat", kwargs={"project_id": project.id}),
    )


def get_run_readiness(
    project: Project,
    *,
    test_run: TestRun | None = None,
    selected_count: int | None = None,
) -> RunReadiness:
    """Compute the "Ready to run?" checklist shown before starting a run."""
    visual_engine = get_visual_engine_status()
    machine_state = get_machine_state(project, visual_engine)
    checks = (
        _cases_selected_check(test_run, selected_count),
        _machine_connected_check(project, machine_state),
        _visual_engine_ready_check(visual_engine),
        _no_active_run_check(project, test_run),
        _no_machine_setup_check(project),
        _application_context_check(project),
    )
    return RunReadiness(checks=checks)


# ============================================================================
# CONTROLLER AGENT SERVICES
# ============================================================================


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def get_project_by_api_key(api_key: str) -> Project | None:
    try:
        return Project.objects.filter(api_key=api_key, archived=False).get()
    except Project.DoesNotExist:
        return None


def regenerate_api_key(project: Project) -> str:
    """Generate and persist a new API key for the given project."""
    project.api_key = generate_api_key()
    project.save()
    return project.api_key


def save_project_prompt(
    *,
    project: Project,
    prompt: str,
    author: CustomUser | None = None,
    source: ContextWriteSource = ContextWriteSource.USER,
) -> None:
    """Replace the notes as a versioned write (raises ``ApplicationContextTooLongError``)."""
    write_application_context(
        project_id=project.pk, text=prompt, source=source, author=author
    )


def save_application_context(
    *,
    project: Project,
    application_url: str,
    application_platform: str,
    project_prompt: str,
    author: CustomUser | None = None,
) -> None:
    """Persist the application context used to brief the AI agent.

    URL and platform are saved as plain fields; the notes go through the
    versioned write, both in one transaction.
    """
    with transaction.atomic():
        project.application_url = application_url
        project.application_platform = application_platform
        project.save(
            update_fields=["application_url", "application_platform", "updated_at"]
        )
        write_application_context(
            project_id=project.pk,
            text=project_prompt,
            source=ContextWriteSource.USER,
            author=author,
        )


def build_agent_project_context(project: Project) -> str | None:
    """Build the context string handed to the agent as its `project_prompt`.

    Combines the application URL, platform, and free-form notes into one
    string. Returns None when there is nothing to tell the agent about the
    application under test.
    """
    lines: list[str] = []
    if project.application_url:
        lines.append(f"Application URL: {project.application_url}")
    if project.application_platform:
        platform_label = ApplicationPlatform(project.application_platform).label
        lines.append(f"Platform: {platform_label}")
    if project.project_prompt.strip():
        lines.append(project.project_prompt.strip())
    if not lines:
        return None
    return "\n".join(lines)


def check_controller_compatibility(
    client_version: str, capabilities: Sequence[str]
) -> str | None:
    """Return a human-readable rejection reason for an incompatible controller.

    Returns None when the controller reports every capability the server
    currently dispatches to. A controller missing one of them would
    otherwise authenticate fine and then silently time out the first time
    the server tries to use that capability.
    """
    missing = _find_missing_capabilities(capabilities)
    if not missing:
        return None

    reported_version = client_version or "unknown"
    return (
        "Controller client is incompatible: missing capabilities "
        f"{', '.join(missing)}. Reported version {reported_version}, "
        f"server expects {CLIENT_VERSION}. Please download a fresh "
        "controller client ZIP from the project page and re-run the "
        "platform setup script."
    )


def _find_missing_capabilities(capabilities: Sequence[str]) -> list[str]:
    present = set(capabilities)
    return sorted(cap for cap in REQUIRED_CLIENT_CAPABILITIES if cap not in present)


def claim_agent_connection(
    project: Project,
    channel_name: str,
    system_info: dict[str, Any],
    client_version: str,
    capabilities: Sequence[str],
) -> str:
    """Make ``channel_name`` the owner of the project's controller connection.

    The newest authenticated connection always wins. Returns the channel name
    of the connection it replaced (so the caller can close it), or "" when
    there was none.
    """
    with transaction.atomic():
        previous_channel_name = (
            Project.objects.select_for_update()
            .filter(pk=project.id)
            .values_list("agent_channel_name", flat=True)
            .get()
        )
        Project.objects.filter(pk=project.id).update(
            agent_connected=True,
            agent_channel_name=channel_name,
            agent_system_info=system_info,
            agent_client_version=client_version,
            agent_capabilities=list(capabilities),
        )
    project.refresh_from_db()
    if previous_channel_name == channel_name:
        return ""
    return previous_channel_name


def release_agent_connection(project_id: int, channel_name: str) -> Project | None:
    """Mark the project disconnected only if ``channel_name`` still owns it.

    Returns the refreshed project when the state changed (so the caller
    broadcasts it), or None when a newer connection already took over.
    """
    rows_updated = Project.objects.filter(
        pk=project_id, agent_channel_name=channel_name
    ).update(**_disconnected_agent_fields())
    if rows_updated == 0:
        return None
    return Project.objects.get(pk=project_id)


def _clear_agent_connection(project_id: int) -> None:
    Project.objects.filter(pk=project_id).update(**_disconnected_agent_fields())


class _DisconnectedAgentFields(TypedDict):
    agent_connected: bool
    agent_channel_name: str
    agent_system_info: dict[str, object]
    agent_client_version: str
    agent_capabilities: list[str]
    last_connected_at: datetime


def _disconnected_agent_fields() -> _DisconnectedAgentFields:
    return {
        "agent_connected": False,
        "agent_channel_name": "",
        "agent_system_info": {},
        "agent_client_version": "",
        "agent_capabilities": [],
        "last_connected_at": timezone.now(),
    }


class AgentStatusEvent(TypedDict):
    type: str
    agent_connected: bool
    agent_system_info: dict[str, Any]
    agent_client_version: str
    agent_capabilities: list[str]
    last_connected_at: str | None


def _agent_status_group(project_id: int) -> str:
    return f"agent_status_{project_id}"


def _build_agent_status_event(project: Project) -> AgentStatusEvent:
    return {
        "type": "agent.status",
        "agent_connected": project.agent_connected,
        "agent_system_info": project.agent_system_info,
        "agent_client_version": project.agent_client_version,
        "agent_capabilities": project.agent_capabilities,
        "last_connected_at": (
            project.last_connected_at.isoformat() if project.last_connected_at else None
        ),
    }


def broadcast_agent_status(project: Project) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event = _build_agent_status_event(project)
    async_to_sync(layer.group_send)(_agent_status_group(project.id), event)


def force_disconnect_controller(project: Project) -> bool:
    """Force-disconnect the controller client and clean up state.

    Sends a close event to the consumer (if alive) and directly
    marks the agent as disconnected to handle zombie connections.
    Returns True if the project was marked connected, False otherwise.
    """
    if not project.agent_connected:
        return False

    # Clear ownership before the close event, so a waiter woken by the closing
    # consumer's CONTROLLER_DISCONNECTED reply never sees the dead connection.
    _clear_agent_connection(project.id)

    layer: Any = get_channel_layer()
    if layer is not None:
        async_to_sync(layer.group_send)(
            _controller_group(project.id),
            {"type": "controller.force_disconnect"},
        )

    project.refresh_from_db()
    broadcast_agent_status(project)
    abort_active_test_run_on_disconnect(project)
    return True


# ============================================================================
# CONTROLLER ACTION SERVICES
# ============================================================================


class ActionResult(TypedDict):
    success: bool
    message: str
    duration_ms: float


class ScreenshotResult(TypedDict):
    success: bool
    image_base64: str
    width: int
    height: int
    format: str


class CommandResult(TypedDict):
    success: bool
    stdout: str
    stderr: str
    return_code: int
    duration_ms: float


def _controller_group(project_id: int) -> str:
    return f"controller_{project_id}"


def _get_channel_layer_or_raise() -> Any:
    layer: Any = get_channel_layer()
    if layer is None:
        raise ControllerActionError("Channel layer is not configured")
    return layer


_ERROR_REPLY_TYPE = "error.result"

_BROWSER_NOT_INSTALLED_MESSAGE = (
    "The browser this case asked for is not installed on the test machine. "
    "Install it (Chrome/Edge: install normally; Firefox/WebKit: "
    "`playwright install firefox webkit` in the Test Runner venv), or run "
    "`python -m controller_client.diagnose --browsers` there."
)


def _raise_for_error_reply(reply: dict[str, Any], event_type: str) -> None:
    """Turn a controller error reply into an exception.

    ``ReplyTracker.send_error`` answers a failed action with an
    ``error.result`` message that carries no ``success`` flag and none of the
    action's own fields. Without this check every caller would read those
    missing fields as their defaults -- an empty screenshot, a zero duration --
    and carry on, so the real controller failure only resurfaced later as an
    unrelated error somewhere downstream.
    """
    if reply.get("type") != _ERROR_REPLY_TYPE:
        return

    message = reply.get("message") or f"Controller failed to handle {event_type}"
    code = reply.get("code", "")
    details = reply.get("details", "")
    if code:
        message = f"{message} [{code}]"
    if details:
        message = f"{message} ({details})"
    if code == ErrorCode.BROWSER_NOT_INSTALLED:
        # The runner's text can carry local paths; it stays in the server log.
        logger.warning("Controller could not launch a browser: %s", message)
        raise BrowserUnavailableError(_BROWSER_NOT_INSTALLED_MESSAGE, detail=message)
    raise ControllerActionError(message)


_T = TypeVar("_T")


class _ConnectionLost(Exception):
    """The connection that received an action dropped before it replied."""


def _run_with_reconnect_retry(
    project_id: int,
    event_type: str,
    attempt: Callable[[str], _T],
    *,
    on_resend: Callable[[], None] | None = None,
    resend: bool = True,
) -> _T:
    """Run ``attempt`` and re-send it once if the Test Runner connection drops.

    ``attempt`` receives the owning consumer channel and raises
    ``_ConnectionLost`` when the loss rule matches. Each attempt dispatches a
    new request on a new reply channel, so a late reply to the abandoned
    request is never read. Any other failure propagates unchanged. With
    ``resend=False`` nothing waits for the runner: a missing runner raises
    ``ControllerNotConnectedError`` and a lost connection fails the action.
    """
    owner = (
        wait_for_controller(project_id)
        if resend
        else _current_owner_or_raise(project_id)
    )
    try:
        return attempt(owner)
    except _ConnectionLost as exc:
        if not resend:
            raise ControllerActionError(
                f"Test Runner connection was lost while running {event_type}"
            ) from exc
        logger.warning(
            "Test Runner connection lost during %s for project %d; "
            "waiting to re-send",
            event_type,
            project_id,
        )

    owner = wait_for_controller(project_id, lost_owner=owner)
    logger.info("Re-sending %s for project %d (attempt 2)", event_type, project_id)
    if on_resend is not None:
        on_resend()
    try:
        return attempt(owner)
    except _ConnectionLost as exc:
        raise TestRunnerDisconnectedError(
            f"Test Runner connection was lost again while running {event_type}; "
            "giving up on this action"
        ) from exc


def _current_owner_or_raise(project_id: int) -> str:
    owner = connected_owner(project_id)
    if owner is None:
        raise ControllerNotConnectedError(
            f"Test Runner is not connected for project {project_id}"
        )
    return owner


def _is_stray_disconnect_reply(
    reply: dict[str, Any], project_id: int, owner: str
) -> bool:
    """Classify a ``CONTROLLER_DISCONNECTED`` reply.

    Raises ``_ConnectionLost`` when the owner the action was sent to is gone.
    Returns True when the owner is still connected: during a takeover the
    action can reach the superseded consumer too, whose close fails it while
    the owner's real reply is still on its way, so the caller keeps waiting.
    """
    if (
        reply.get("type") != _ERROR_REPLY_TYPE
        or reply.get("code") != ErrorCode.CONTROLLER_DISCONNECTED
    ):
        return False
    if controller_connection_lost(project_id, owner):
        raise _ConnectionLost(reply.get("message", ""))
    return True


def _raise_reply_timeout(
    project_id: int,
    owner: str,
    message: str,
    cause: BaseException | None,
    *,
    runner_local: bool = False,
) -> NoReturn:
    """Raise ``_ConnectionLost`` if the owner changed, else a timeout error.

    ``runner_local`` marks actions whose duration does not depend on the
    application under test (input, screenshot, cleanup, ...):
    their silence means the Test Runner itself stopped responding, which blocks
    the case. Other timeouts stay plain so the agent can judge a hung app.
    """
    if controller_connection_lost(project_id, owner):
        raise _ConnectionLost(message) from cause
    if runner_local:
        raise TestRunnerUnresponsiveError(message) from cause
    raise ControllerActionError(message) from cause


def _dispatch_controller_action(
    project_id: int,
    event_type: str,
    reply_timeout: float,
    *,
    resend_on_reconnect: bool = True,
    runner_local: bool = False,
    **payload: Any,
) -> dict[str, Any]:
    layer = _get_channel_layer_or_raise()

    def _attempt(owner: str) -> dict[str, Any]:
        return _send_controller_action_once(
            layer,
            project_id,
            owner,
            event_type,
            reply_timeout,
            payload,
            runner_local=runner_local,
        )

    return _run_with_reconnect_retry(
        project_id, event_type, _attempt, resend=resend_on_reconnect
    )


def _send_controller_action_once(
    layer: Any,
    project_id: int,
    owner: str,
    event_type: str,
    reply_timeout: float,
    payload: dict[str, Any],
    *,
    runner_local: bool,
) -> dict[str, Any]:
    reply_channel: str = async_to_sync(layer.new_channel)()
    request_id = str(uuid.uuid4())

    event: dict[str, Any] = {
        "type": event_type,
        "request_id": request_id,
        "reply_channel": reply_channel,
        **payload,
    }
    async_to_sync(layer.group_send)(_controller_group(project_id), event)

    deadline = time.monotonic() + reply_timeout
    timeout_message = (
        f"Timed out waiting for controller reply to {event_type} "
        f"after {reply_timeout}s"
    )
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _raise_reply_timeout(
                project_id, owner, timeout_message, None, runner_local=runner_local
            )

        try:
            result: dict[str, Any] = async_to_sync(asyncio.wait_for)(
                layer.receive(reply_channel), timeout=remaining
            )
        except asyncio.TimeoutError as exc:
            _raise_reply_timeout(
                project_id, owner, timeout_message, exc, runner_local=runner_local
            )

        if _is_stray_disconnect_reply(result, project_id, owner):
            continue
        _raise_for_error_reply(result, event_type)
        return result


def _build_action_result(reply: dict[str, Any]) -> ActionResult:
    return ActionResult(
        success=reply.get("success", False),
        message=reply.get("message", ""),
        duration_ms=reply.get("duration_ms", 0.0),
    )


def controller_click(
    project_id: int,
    x: int,
    y: int,
    button: str = "left",
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.click",
        timeout,
        x=x,
        y=y,
        button=button,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_double_click(
    project_id: int,
    x: int,
    y: int,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.double_click",
        timeout,
        x=x,
        y=y,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_hover(
    project_id: int,
    x: int,
    y: int,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.hover",
        timeout,
        x=x,
        y=y,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_drag(
    project_id: int,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str = "left",
    duration: float = 0.5,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.drag",
        timeout,
        runner_local=True,
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
        button=button,
        duration=duration,
    )
    return _build_action_result(reply)


def controller_type_text(
    project_id: int,
    text: str,
    interval: float = 0.0,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.type_text",
        timeout,
        text=text,
        interval=interval,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_key_press(
    project_id: int,
    keys: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.key_press",
        timeout,
        keys=keys,
        runner_local=True,
    )
    return _build_action_result(reply)


def _build_screenshot_result(reply: dict[str, Any], action: str) -> ScreenshotResult:
    """Build a screenshot result, refusing to hand back an empty image.

    An image-less screenshot used to travel on until the vision model was
    asked about a zero-byte data URL, which the provider rejected as a 400 --
    an error that named the LLM endpoint instead of the controller.
    """
    if not reply.get("success", False):
        message = reply.get("message") or f"Controller failed to {action}"
        raise ControllerActionError(message)

    image_base64 = reply.get("image_base64", "")
    if not image_base64:
        raise ControllerActionError(f"Controller returned an empty image for {action}")

    return ScreenshotResult(
        success=True,
        image_base64=image_base64,
        width=reply.get("width", 0),
        height=reply.get("height", 0),
        format=reply.get("format", "png"),
    )


_SCREENSHOT_ATTEMPTS: Final = 3


def controller_screenshot(
    project_id: int,
    timeout: float = 30.0,
) -> ScreenshotResult:
    """Take a desktop screenshot, re-asking up to ``_SCREENSHOT_ATTEMPTS`` times.

    A multi-MiB image on a slow uplink can miss one reply window while the
    runner is fine. Screenshots
    are idempotent, so a timeout is re-sent instead of pausing the case at
    once; only the last timeout propagates as ``TestRunnerUnresponsiveError``.
    """
    for attempt in range(1, _SCREENSHOT_ATTEMPTS):
        try:
            reply = _dispatch_controller_action(
                project_id, "controller.screenshot", timeout, runner_local=True
            )
        except TestRunnerUnresponsiveError:
            logger.warning(
                "Screenshot timed out for project %d (attempt %d/%d); retrying",
                project_id,
                attempt,
                _SCREENSHOT_ATTEMPTS,
            )
            continue
        return _build_screenshot_result(reply, "take a screenshot")

    try:
        reply = _dispatch_controller_action(
            project_id, "controller.screenshot", timeout, runner_local=True
        )
    except TestRunnerUnresponsiveError as exc:
        raise TestRunnerUnresponsiveError(
            f"Test Runner did not return a screenshot after "
            f"{_SCREENSHOT_ATTEMPTS} attempts of {timeout:.0f}s each"
        ) from exc
    return _build_screenshot_result(reply, "take a screenshot")


def controller_find_elements(
    project_id: int,
    box_threshold: float | None = None,
    iou_threshold: float | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> PixelParseResult:
    """Screenshot the test machine, then have a vision worker parse it.

    The elements are in the screenshot's pixel space, which is the space the
    runner's clicks use, so callers use their centers unchanged.
    """
    shot = controller_screenshot(project_id)
    return parse_screenshot(
        shot["image_base64"],
        image_width=shot["width"],
        image_height=shot["height"],
        box_threshold=box_threshold,
        iou_threshold=iou_threshold,
        cancellation_check=cancellation_check,
    )


_RUN_COMMAND_EVENT = "controller.run_command"
_RERUN_COMMAND_MARKER = "[Test Runner reconnected - re-running command]"


def controller_run_command(
    project_id: int,
    command: str,
) -> CommandResult:
    return controller_run_command_streaming(project_id, command)


def controller_run_command_streaming(
    project_id: int,
    command: str,
    on_output: Callable[[str, str], None] | None = None,
) -> CommandResult:
    layer = _get_channel_layer_or_raise()
    timeout = float(settings.AGENT_TIMEOUT_SECONDS)

    def _attempt(owner: str) -> CommandResult:
        reply_channel: str = async_to_sync(layer.new_channel)()
        request_id = str(uuid.uuid4())
        _dispatch_run_command_event(
            layer, project_id, request_id, reply_channel, command
        )
        return _receive_streaming_command_result(
            layer, project_id, owner, reply_channel, timeout, on_output
        )

    def _emit_rerun_marker() -> None:
        if on_output is not None:
            on_output(_RERUN_COMMAND_MARKER, "stderr")

    return _run_with_reconnect_retry(
        project_id, _RUN_COMMAND_EVENT, _attempt, on_resend=_emit_rerun_marker
    )


def _dispatch_run_command_event(
    layer: Any,
    project_id: int,
    request_id: str,
    reply_channel: str,
    command: str,
) -> None:
    event: dict[str, Any] = {
        "type": _RUN_COMMAND_EVENT,
        "request_id": request_id,
        "reply_channel": reply_channel,
        "command": command,
    }
    async_to_sync(layer.group_send)(_controller_group(project_id), event)


def _receive_streaming_command_result(
    layer: Any,
    project_id: int,
    owner: str,
    reply_channel: str,
    timeout: float,
    on_output: Callable[[str, str], None] | None,
) -> CommandResult:
    deadline = time.monotonic() + timeout
    timeout_message = f"Timed out waiting for command result after {timeout}s"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _raise_reply_timeout(project_id, owner, timeout_message, None)

        try:
            reply: dict[str, Any] = async_to_sync(asyncio.wait_for)(
                layer.receive(reply_channel), timeout=remaining
            )
        except asyncio.TimeoutError as exc:
            _raise_reply_timeout(project_id, owner, timeout_message, exc)

        if _is_stray_disconnect_reply(reply, project_id, owner):
            continue
        _raise_for_error_reply(reply, _RUN_COMMAND_EVENT)

        msg_type = reply.get("type", "")
        if msg_type == "command.output":
            if on_output is not None:
                line = str(reply.get("line", ""))
                stream = str(reply.get("stream", "stdout"))
                on_output(line, stream)
            continue

        if msg_type == "command.result":
            return _build_command_result(reply)

        logger.warning("Unexpected message type in streaming receive: %s", msg_type)


def _build_command_result(reply: dict[str, Any]) -> CommandResult:
    return CommandResult(
        success=reply.get("success", False),
        stdout=reply.get("stdout", ""),
        stderr=reply.get("stderr", ""),
        return_code=reply.get("return_code", -1),
        duration_ms=reply.get("duration_ms", 0.0),
    )


# ============================================================================
# INTERACTIVE COMMAND SERVICES
# ============================================================================


class InteractiveCommandResult(TypedDict):
    session_id: str
    output: str
    is_alive: bool
    exit_code: int | None
    duration_ms: float


def _build_interactive_command_result(
    reply: dict[str, Any],
) -> InteractiveCommandResult:
    return InteractiveCommandResult(
        session_id=reply.get("session_id", ""),
        output=reply.get("output", ""),
        is_alive=reply.get("is_alive", False),
        exit_code=reply.get("exit_code"),
        duration_ms=reply.get("duration_ms", 0.0),
    )


def controller_start_interactive_command(
    project_id: int,
    command: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.start_interactive_cmd",
        float(settings.INTERACTIVE_CMD_TIMEOUT_SECONDS),
        command=command,
    )
    return _build_interactive_command_result(reply)


def controller_send_input(
    project_id: int,
    session_id: str,
    input_text: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.send_input",
        float(settings.INTERACTIVE_CMD_TIMEOUT_SECONDS),
        session_id=session_id,
        input_text=input_text,
    )
    return _build_interactive_command_result(reply)


def controller_wait_for_command(
    project_id: int,
    session_id: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.wait_for_command",
        float(settings.INTERACTIVE_CMD_TIMEOUT_SECONDS),
        session_id=session_id,
    )
    return _build_interactive_command_result(reply)


def controller_terminate_interactive_command(
    project_id: int,
    session_id: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.terminate_interactive_cmd",
        _INTERACTIVE_TERMINATE_TIMEOUT_SECONDS,
        session_id=session_id,
    )
    return _build_interactive_command_result(reply)


# ============================================================================
# BROWSER ACTION SERVICES
# ============================================================================


class BrowserContentResult(TypedDict):
    success: bool
    content: str
    duration_ms: float


def _build_browser_content_result(reply: dict[str, Any]) -> BrowserContentResult:
    return BrowserContentResult(
        success=reply.get("success", False),
        content=reply.get("content", ""),
        duration_ms=reply.get("duration_ms", 0.0),
    )


def controller_browser_navigate(
    project_id: int,
    url: str,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_navigate",
        timeout,
        url=url,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_click(
    project_id: int,
    element_index: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_click",
        timeout,
        element_index=element_index,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_double_click(
    project_id: int,
    element_index: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_double_click",
        timeout,
        element_index=element_index,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_type(
    project_id: int,
    element_index: int,
    text: str,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_type",
        timeout,
        element_index=element_index,
        text=text,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_hover(
    project_id: int,
    element_index: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_hover",
        timeout,
        element_index=element_index,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_get_elements(
    project_id: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_get_elements",
        timeout,
        browser=browser.value,
    )
    return _build_browser_content_result(reply)


def controller_browser_get_page_content(
    project_id: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_get_page_content",
        timeout,
        browser=browser.value,
    )
    return _build_browser_content_result(reply)


def controller_browser_get_url(
    project_id: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_get_url",
        timeout,
        browser=browser.value,
    )
    return _build_browser_content_result(reply)


def controller_browser_download(
    project_id: int,
    url: str,
    save_path: str = "",
    *,
    browser: BrowserName,
    timeout: float = 120.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_download",
        timeout,
        url=url,
        save_path=save_path,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_list_downloads(
    project_id: int,
    timeout: float = 310.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_list_downloads", timeout
    )
    return _build_action_result(reply)


def controller_launch_app(
    project_id: int,
    app_name: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.launch_app", timeout, app_name=app_name
    )
    return _build_action_result(reply)


def controller_close_app(
    project_id: int,
    app_name: str,
    *,
    force: bool = False,
    timeout: float = 30.0,
) -> ActionResult:
    # The 30 s timeout is well above the runner's 5 s wait for the app to quit.
    reply = _dispatch_controller_action(
        project_id,
        "controller.close_app",
        timeout,
        app_name=app_name,
        force=force,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_close_tab(
    project_id: int,
    app_name: str,
    *,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.close_tab",
        timeout,
        app_name=app_name,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_check_app_installed(
    project_id: int,
    app_name: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.check_app_installed",
        timeout,
        app_name=app_name,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_cleanup_environment(
    project_id: int,
    timeout: float = 30.0,
    *,
    resend_on_reconnect: bool = True,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.cleanup_environment",
        timeout,
        resend_on_reconnect=resend_on_reconnect,
        runner_local=True,
    )
    return _build_action_result(reply)


def _safe_cleanup(project_id: int, *, wait_for_runner: bool) -> None:
    """Empty the test machine's cleanup folder, never failing the test case.

    Before a case (``wait_for_runner=True``) a dropped runner is waited for like
    any action, and a grace expiry, abort or missing runner propagates so the
    run is aborted or cancelled. After a case nothing waits for the runner: a
    missing runner or a drop during the cleanup is only logged.
    """
    try:
        timeout = float(settings.CLEANUP_TIMEOUT_SECONDS)
        result = controller_cleanup_environment(
            project_id, timeout=timeout, resend_on_reconnect=wait_for_runner
        )
        if result["success"]:
            logger.info(
                "Environment cleanup for project %d: %s", project_id, result["message"]
            )
        else:
            logger.warning(
                "Environment cleanup for project %d left work undone: %s",
                project_id,
                result["message"],
            )
    except (
        ControllerReconnectTimeoutError,
        AgentCancelledError,
        ControllerNotConnectedError,
        SoftTimeLimitExceeded,
    ):
        if wait_for_runner:
            raise
        logger.warning(
            "Environment cleanup skipped for project %d", project_id, exc_info=True
        )
    except Exception:
        logger.warning(
            "Environment cleanup failed for project %d", project_id, exc_info=True
        )


def _safe_teardown(
    pivot: TestRunTestCase,
    project: Project,
    *,
    generation: int,
    cancellation_check: Callable[[], bool],
    run_case_teardown: _CaseTeardown,
) -> None:
    """Close the apps a Failed case left open, never failing the case.

    Runs after the outcome is written and before ``_safe_cleanup``, so apps that
    hold files in the cleanup folder are gone before it is emptied.
    """
    timeout = settings.CASE_TEARDOWN_TIMEOUT_SECONDS
    if timeout <= 0:
        return
    try:
        pivot.refresh_from_db(fields=["status"])
        if not needs_teardown(pivot.status):
            return
        checkpoint = load_resume_checkpoint(pivot)
        if checkpoint is None:
            logger.info("Teardown skipped for pivot %d: no plan", pivot.pk)
            return
        run_case_teardown(
            checkpoint,
            project.id,
            timeout_seconds=timeout,
            on_log=_build_log_callback(pivot, generation),
            cancellation_check=cancellation_check,
            system_info=project.agent_system_info or None,
            project_prompt=build_agent_project_context(project),
            machine_ready=lambda: _machine_can_close_apps(project.id),
        )
    except Exception:
        logger.warning("Teardown failed for pivot %d", pivot.pk, exc_info=True)


def _machine_can_close_apps(project_id: int) -> bool:
    """Runner connected with ``close_app_v1`` and a vision worker ready."""
    connected, capabilities = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_connected", "agent_capabilities")
        .get()
    )
    return (
        connected
        and ClientCapability.CLOSE_APP_V1.value in (capabilities or [])
        and bool(list_ready_workers())
    )


def controller_browser_take_screenshot(
    project_id: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ScreenshotResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_take_screenshot",
        timeout,
        browser=browser.value,
    )
    return _build_screenshot_result(reply, "take a browser screenshot")


# ============================================================================
# TEST CASE SERVICES
# ============================================================================


def create_test_case(*, project: Project, data: TestCaseData) -> TestCase:
    return TestCase.objects.create(project=project, **asdict(data))


def can_edit_test_case_in_run(test_run: TestRun) -> bool:
    return test_run.status not in ACTIVE_RUN_STATUSES


def update_test_case(*, test_case: TestCase, data: TestCaseData) -> TestCase:
    for field, value in asdict(data).items():
        setattr(test_case, field, value)
    test_case.save()
    return test_case


def delete_test_case(test_case: TestCase) -> None:
    test_case.delete()


def bulk_delete_test_cases(*, project: Project, test_case_ids: list[int]) -> int:
    """Delete the given test cases (scoped to the project) and return how many."""
    queryset = TestCase.objects.filter(id__in=test_case_ids, project=project)
    count = queryset.count()
    queryset.delete()
    return count


def get_test_case_for_project(test_case_id: int, project: Project) -> TestCase | None:
    try:
        return TestCase.objects.filter(id=test_case_id, project=project).get()
    except TestCase.DoesNotExist:
        return None


VALID_STATUS_FILTERS: frozenset[str] = frozenset(
    choice.value for choice in TestRunTestCaseStatus
) | frozenset({"never_run"})

STATUS_FILTER_CHOICES: list[tuple[str, str]] = [
    ("", "All Statuses"),
    ("never_run", "Never Run"),
] + [(choice.value, choice.label) for choice in TestRunTestCaseStatus]


def list_test_cases_for_project(
    *,
    project: Project,
    search: str | None,
    page: int,
    per_page: int,
    upload_id: int | None = None,
    status_filter: str | None = None,
) -> Page[TestCase]:
    """Return a paginated list of test cases for a project, optionally filtered."""
    latest_pivot = TestRunTestCase.objects.filter(
        test_case=OuterRef("pk"),
    ).order_by("-created_at")

    qs: QuerySet[TestCase] = (
        TestCase.objects.filter(project=project)
        .annotate(
            last_run_pivot_id=Subquery(latest_pivot.values("pk")[:1]),
            last_run_status=Subquery(latest_pivot.values("status")[:1]),
            last_run_test_run_id=Subquery(latest_pivot.values("test_run_id")[:1]),
            last_run_at=Coalesce(
                Subquery(latest_pivot.values("finished_at")[:1]),
                Subquery(latest_pivot.values("created_at")[:1]),
            ),
        )
        .order_by("-created_at")
    )

    if search:
        qs = qs.filter(title__icontains=search)

    if upload_id is not None:
        qs = qs.filter(upload_id=upload_id)

    if status_filter and status_filter in VALID_STATUS_FILTERS:
        if status_filter == "never_run":
            qs = qs.filter(**{"last_run_pivot_id__isnull": True})
        else:
            qs = qs.filter(**{"last_run_status": status_filter})

    paginator: Paginator[TestCase] = Paginator(qs, per_page)
    return paginator.get_page(page)


# ============================================================================
# UPLOAD MANAGEMENT SERVICES
# ============================================================================


def cancel_upload_processing(upload: TestCaseUpload) -> None:
    """Revoke Celery task if running, delete partial test cases, set status cancelled."""
    celery_app.control.revoke(upload.celery_task_id, terminate=True)
    upload.test_cases.all().delete()
    upload.status = UploadStatus.CANCELLED
    upload.save()


def delete_upload(upload: TestCaseUpload) -> None:
    """Cancel if processing, delete file if exists, then delete upload record."""
    if upload.status == UploadStatus.PROCESSING:
        cancel_upload_processing(upload)

    if upload.file:
        upload.file.delete(save=False)

    upload.delete()


def get_upload_for_project(upload_id: int, project: Project) -> TestCaseUpload | None:
    """Get upload by id for a specific project, or None."""
    try:
        return TestCaseUpload.objects.filter(id=upload_id, project=project).get()
    except TestCaseUpload.DoesNotExist:
        return None


def list_uploads_for_project(
    *, project: Project, page: int, per_page: int
) -> Page[TestCaseUpload]:
    """Return a paginated list of uploads for a project, ordered by newest first."""
    qs: QuerySet[TestCaseUpload] = TestCaseUpload.objects.filter(
        project=project
    ).order_by("-created_at")
    paginator: Paginator[TestCaseUpload] = Paginator(qs, per_page)
    return paginator.get_page(page)


def list_completed_uploads_for_project(*, project: Project) -> QuerySet[TestCaseUpload]:
    return TestCaseUpload.objects.filter(
        project=project, status=UploadStatus.COMPLETED
    ).order_by("-created_at")


# ============================================================================
# TEST RUN UI SERVICES
# ============================================================================


@transaction.atomic
def create_test_run_with_cases(
    *, project: Project, test_case_ids: list[int], name: str = ""
) -> TestRun:
    test_run = TestRun.objects.create(project=project, name=name)
    valid_cases = TestCase.objects.filter(id__in=test_case_ids, project=project).only(
        "id", "testrail_source_run_id"
    )
    pivots = [
        TestRunTestCase(
            test_run=test_run,
            test_case=tc,
            testrail_source_run_id=tc.testrail_source_run_id,
        )
        for tc in valid_cases
    ]
    TestRunTestCase.objects.bulk_create(pivots)
    return test_run


def suggest_run_name(project: Project) -> str:
    """Suggest "Run N" from the run count; a suggestion only, uniqueness is not guaranteed."""
    existing_count = TestRun.objects.filter(project=project).count()
    return f"Run {existing_count + 1}"


@transaction.atomic
def add_cases_to_test_run(*, test_run: TestRun, test_case_ids: list[int]) -> int:
    valid_cases = TestCase.objects.filter(
        id__in=test_case_ids, project=test_run.project
    )
    existing_case_ids = set(
        test_run.pivot_entries.values_list("test_case_id", flat=True)
    )
    new_pivots = [
        TestRunTestCase(
            test_run=test_run,
            test_case=tc,
            testrail_source_run_id=tc.testrail_source_run_id,
        )
        for tc in valid_cases
        if tc.id not in existing_case_ids
    ]
    TestRunTestCase.objects.bulk_create(new_pivots)
    return len(new_pivots)


def list_test_runs_for_project(
    *, project: Project, page: int, per_page: int
) -> Page[TestRun]:
    qs = _annotate_run_counts(TestRun.objects.filter(project=project)).order_by(
        "-created_at"
    )
    paginator: Paginator[TestRun] = Paginator(qs, per_page)
    return paginator.get_page(page)


def get_test_run_for_project(test_run_id: int, project: Project) -> TestRun | None:
    try:
        return TestRun.objects.filter(id=test_run_id, project=project).get()
    except TestRun.DoesNotExist:
        return None


def get_case_position(pivot: TestRunTestCase) -> tuple[int, int]:
    """Return the pivot's 1-based position and total case count within its run.

    Uses the run order (`RUN_CASE_ORDERING`, the order cases execute in)
    rather than the failures-first ordering `list_test_run_cases` uses for
    display, since that ordering is not a stable position for a given pivot.
    One query: a run holds at most a few hundred pivot IDs.
    """
    ordered_ids = list(
        TestRunTestCase.objects.filter(test_run_id=pivot.test_run_id)
        .order_by(*RUN_CASE_ORDERING)
        .values_list("id", flat=True)
    )
    return ordered_ids.index(pivot.id) + 1, len(ordered_ids)


def get_test_run_case_detail(pivot_id: int, project: Project) -> TestRunTestCase | None:
    try:
        return (
            TestRunTestCase.objects.select_related(
                "test_run", "test_case", "checked_by"
            )
            .prefetch_related(
                Prefetch(
                    "screenshots",
                    queryset=TestRunScreenshot.objects.order_by("created_at"),
                )
            )
            .filter(id=pivot_id, test_run__project=project)
            .get()
        )
    except TestRunTestCase.DoesNotExist:
        return None


def list_test_run_cases(
    *, test_run: TestRun, page: int, per_page: int
) -> Page[TestRunTestCase]:
    """List a run's cases: failed, then blocked, then the rest.

    TestRail order within each group.
    """
    qs = (
        test_run.pivot_entries.select_related("test_case", "checked_by")
        .annotate(
            _failure_priority=Case(
                When(status=TestRunTestCaseStatus.FAILED, then=0),
                When(status=TestRunTestCaseStatus.BLOCKED, then=1),
                default=2,
                output_field=IntegerField(),
            )
        )
        .order_by("_failure_priority", *RUN_CASE_ORDERING)
    )
    paginator: Paginator[TestRunTestCase] = Paginator(qs, per_page)
    return paginator.get_page(page)


def list_waiting_test_runs_for_project(project: Project) -> QuerySet[TestRun]:
    return TestRun.objects.filter(
        project=project, status=TestRunStatus.WAITING
    ).order_by("-created_at")


def _can_modify_test_run(test_run: TestRun) -> bool:
    return test_run.status == TestRunStatus.WAITING


def delete_test_run(test_run: TestRun) -> None:
    if not _can_modify_test_run(test_run):
        raise ValueError("Only waiting test runs can be deleted.")
    test_run.delete()


def remove_case_from_test_run(test_run: TestRun, pivot_id: int) -> None:
    if not _can_modify_test_run(test_run):
        raise ValueError("Cannot remove cases from a non-waiting test run.")
    test_run.pivot_entries.filter(id=pivot_id).delete()


@transaction.atomic
def reset_test_run(test_run: TestRun) -> None:
    # Lock the row so a concurrent "Send checked to TestRail" cannot take the
    # push slot between this check and the reset (its slot UPDATE waits, then
    # no longer matches a WAITING run).
    locked = (
        TestRun.objects.select_for_update()
        .only(
            "id",
            "status",
            "testrail_source_push_status",
            "testrail_source_push_started_at",
        )
        .get(pk=test_run.pk)
    )
    if locked.status not in (TestRunStatus.DONE, TestRunStatus.CANCELLED):
        raise ValueError("Only completed or cancelled test runs can be reset.")
    # A stale PUSHING (killed worker) does not block the reset.
    if is_checked_push_live(locked, timezone.now()):
        raise ValueError("Wait for the checked results to finish sending to TestRail.")
    for pivot in test_run.pivot_entries.all():
        for screenshot in pivot.screenshots.all():
            screenshot.image.delete(save=False)
        pivot.screenshots.all().delete()
        pivot.status = TestRunTestCaseStatus.CREATED
        pivot.blocked_reason = ""
        pivot.result = ""
        pivot.logs = ""
        pivot.started_at = None
        pivot.finished_at = None
        pivot.checked_at = None
        pivot.checked_by = None
        pivot.testrail_source_result_id = None
        pivot.testrail_source_sent_at = None
        pivot.testrail_source_attachment_count = 0
        pivot.testrail_source_skip_reason = ""
        pivot.execution_checkpoint = {}
        pivot.save(
            update_fields=[
                "status",
                "blocked_reason",
                "result",
                "logs",
                "started_at",
                "finished_at",
                "checked_at",
                "checked_by",
                "testrail_source_result_id",
                "testrail_source_sent_at",
                "testrail_source_attachment_count",
                "testrail_source_skip_reason",
                "execution_checkpoint",
                "updated_at",
            ]
        )
    TestRunCaseResume.objects.filter(test_run_test_case__test_run=test_run).delete()
    test_run.status = TestRunStatus.WAITING
    test_run.celery_task_id = ""
    test_run.started_at = None
    test_run.finished_at = None
    test_run.testrail_push_status = TestRailPushStatus.NOT_PUSHED
    test_run.testrail_pushed_at = None
    test_run.testrail_push_error = ""
    test_run.testrail_push_summary = ""
    test_run.testrail_source_push_status = TestRailPushStatus.NOT_PUSHED
    test_run.testrail_source_push_started_at = None
    test_run.testrail_source_pushed_at = None
    test_run.testrail_source_push_error = ""
    test_run.testrail_source_push_summary = ""
    test_run.save(
        update_fields=[
            "status",
            "celery_task_id",
            "started_at",
            "finished_at",
            "testrail_push_status",
            "testrail_pushed_at",
            "testrail_push_error",
            "testrail_push_summary",
            "testrail_source_push_status",
            "testrail_source_push_started_at",
            "testrail_source_pushed_at",
            "testrail_source_push_error",
            "testrail_source_push_summary",
            "updated_at",
        ]
    )


def _has_active_test_run(project: Project) -> bool:
    return TestRun.objects.filter(
        project=project, status__in=ACTIVE_RUN_STATUSES
    ).exists()


@transaction.atomic
def start_test_run(test_run: TestRun, *, help_mode: str) -> None:
    """Start a WAITING run with ``help_mode`` (a ``RunHelpMode`` value)."""
    if help_mode not in RunHelpMode.values:
        raise ValueError("Choose what happens when a case needs help.")
    # Same lock order as resume (project, then run): it serializes a Start
    # against another Start or Resume in the project, so the "no other run is
    # active" check below cannot race.
    Project.objects.select_for_update().only("id").get(pk=test_run.project_id)
    locked_status = (
        TestRun.objects.select_for_update()
        .values_list("status", flat=True)
        .get(pk=test_run.pk)
    )

    pivot_ids = list(
        test_run.pivot_entries.order_by(*RUN_CASE_ORDERING).values_list("id", flat=True)
    )
    if not pivot_ids:
        raise ValueError("Cannot start a test run with no test cases.")
    if locked_status != TestRunStatus.WAITING:
        raise ValueError("Test run is not in WAITING status.")
    if _has_active_test_run(test_run.project):
        raise ValueError("Another test run is already in progress for this project.")
    if live_turn_exists(test_run.project_id, timezone.now()):
        raise ValueError(MACHINE_SETUP_RUNNING_MESSAGE)

    test_run.status = TestRunStatus.STARTED
    test_run.help_mode = help_mode
    test_run.started_at = timezone.now()
    test_run.finished_at = None
    test_run.execution_generation += 1
    test_run.save(
        update_fields=[
            "status",
            "help_mode",
            "started_at",
            "finished_at",
            "execution_generation",
            "updated_at",
        ]
    )

    def _enqueue() -> None:
        if queue_test_run_cases(test_run, pivot_ids):
            broadcast_test_run_status(test_run)

    # Workers must see the STARTED run, so the chain is queued after commit.
    transaction.on_commit(_enqueue)


def queue_test_run_cases(test_run: TestRun, pivot_ids: Sequence[int]) -> bool:
    """Chain the cases on the execution queue, tagged with the run's generation.

    Call after commit, before broadcasting. If the broker refuses the chain,
    the run is cancelled (best effort) so it does not sit in STARTED with
    nobody executing it, and False is returned.
    """
    from projects.tasks import execute_test_run_case

    generation = test_run.execution_generation
    try:
        result = chain(
            *[execute_test_run_case.si(pid, generation) for pid in pivot_ids]
        ).apply_async()
    except Exception:
        logger.exception("Could not queue test run %d", test_run.pk)
        try:
            abort_test_run(
                test_run, reason="Could not queue the run", revoke_task=False
            )
        except Exception:
            # The channel layer shares the broker's Redis; the run is already
            # CANCELLED in the database when only the broadcast fails.
            logger.exception("Could not cancel unqueued test run %d", test_run.pk)
        return False
    test_run.celery_task_id = result.id
    test_run.save(update_fields=["celery_task_id", "updated_at"])
    return True


def get_test_run_summary(test_run: TestRun) -> dict[str, int]:
    result = test_run.pivot_entries.aggregate(
        total=Count("id"),
        created=Count("id", filter=Q(status=TestRunTestCaseStatus.CREATED)),
        in_progress=Count("id", filter=Q(status=TestRunTestCaseStatus.IN_PROGRESS)),
        success=Count("id", filter=Q(status=TestRunTestCaseStatus.SUCCESS)),
        failed=Count("id", filter=Q(status=TestRunTestCaseStatus.FAILED)),
        blocked=Count("id", filter=Q(status=TestRunTestCaseStatus.BLOCKED)),
        cancelled=Count("id", filter=Q(status=TestRunTestCaseStatus.CANCELLED)),
        need_help=Count("id", filter=Q(status=TestRunTestCaseStatus.NEED_HELP)),
    )
    return {k: v or 0 for k, v in result.items()}


class RerunScope(StrEnum):
    """Which cases of a finished run a re-run copies."""

    FAILED = "failed"
    BLOCKED = "blocked"


_RERUN_SCOPE_STATUS: dict[RerunScope, TestRunTestCaseStatus] = {
    RerunScope.FAILED: TestRunTestCaseStatus.FAILED,
    RerunScope.BLOCKED: TestRunTestCaseStatus.BLOCKED,
}


def can_rerun_cases(test_run: TestRun, count: int) -> bool:
    """Single source of the rerun rule, shared by the UI gate and the service."""
    return (
        test_run.status in (TestRunStatus.DONE, TestRunStatus.CANCELLED) and count > 0
    )


@transaction.atomic
def rerun_cases(test_run: TestRun, scope: RerunScope) -> TestRun:
    """Create a new run containing only the `scope` cases from `test_run`.

    Only DONE/CANCELLED runs qualify — a run still in progress has no stable
    set of cases to copy yet. The TestRail target is copied so pushing the new
    run adds its verdicts to the same TestRail run, and each case keeps its
    source TestRail run so its checked result can be sent there.
    """
    rows = list(
        test_run.pivot_entries.filter(status=_RERUN_SCOPE_STATUS[scope]).values_list(
            "test_case_id", "testrail_source_run_id"
        )
    )
    if not can_rerun_cases(test_run, len(rows)):
        raise ValueError(
            f"Only completed or cancelled runs with {scope.value} cases can be rerun."
        )

    new_run = TestRun.objects.create(
        project=test_run.project,
        # Pre-selects the source run's mode at Start.
        help_mode=test_run.help_mode,
        name=f"{test_run.display_name} — {scope.value} rerun",
        testrail_project_id=test_run.testrail_project_id,
        testrail_suite_id=test_run.testrail_suite_id,
        testrail_run_id=test_run.testrail_run_id,
    )
    pivots = [
        TestRunTestCase(
            test_run=new_run, test_case_id=case_id, testrail_source_run_id=source_run_id
        )
        for case_id, source_run_id in rows
    ]
    TestRunTestCase.objects.bulk_create(pivots)
    return new_run


NARRATIVE_TITLE_LIMIT = 10


def _join_titles(titles: list[str], count: int) -> str:
    joined = ", ".join(titles)
    if count > len(titles):
        joined += f" and {count - len(titles)} more"
    return joined


def list_failed_case_titles(test_run: TestRun) -> list[str]:
    """First failed case titles of a run, for the "What happened" summary."""
    return list(
        test_run.pivot_entries.filter(status=TestRunTestCaseStatus.FAILED)
        .order_by(*RUN_CASE_ORDERING)
        .values_list("test_case__title", flat=True)[:NARRATIVE_TITLE_LIMIT]
    )


def list_blocked_case_titles(test_run: TestRun) -> list[str]:
    """First blocked case titles of a run, for the "What happened" summary."""
    return list(
        test_run.pivot_entries.filter(status=TestRunTestCaseStatus.BLOCKED)
        .order_by(*RUN_CASE_ORDERING)
        .values_list("test_case__title", flat=True)[:NARRATIVE_TITLE_LIMIT]
    )


def _build_narrative_for_active_run(test_run: TestRun, summary: dict[str, int]) -> str:
    total = summary.get("total", 0)
    finished = (
        summary.get("success", 0) + summary.get("failed", 0) + summary.get("blocked", 0)
    )
    if test_run.status == TestRunStatus.CANCELLED:
        return f"Run cancelled after {finished} of {total} cases finished."
    if test_run.status == TestRunStatus.PAUSED:
        need_help = summary.get("need_help", 0)
        noun = "case needs" if need_help == 1 else "cases need"
        return (
            f"Paused: {need_help} {noun} help. "
            f"{finished} of {total} cases finished so far."
        )
    return f"{finished} of {total} cases finished so far."


def _build_narrative_for_finished_run(
    summary: dict[str, int], failed_titles: list[str], blocked_titles: list[str]
) -> str:
    total = summary.get("total", 0)
    success = summary.get("success", 0)
    if total > 0 and success == total:
        return f"All {total} cases passed."

    sentence = f"{success} of {total} cases passed."
    failed_count = summary.get("failed", 0)
    if failed_count and failed_titles:
        sentence += (
            f" {failed_count} failed: {_join_titles(failed_titles, failed_count)}."
        )
    elif failed_count:
        sentence += f" {failed_count} failed."
    blocked_count = summary.get("blocked", 0)
    if blocked_count and blocked_titles:
        sentence += (
            f" {blocked_count} blocked (not judged): "
            f"{_join_titles(blocked_titles, blocked_count)}."
        )
    elif blocked_count:
        sentence += f" {blocked_count} blocked (not judged)."
    return sentence


def build_run_narrative(
    test_run: TestRun,
    summary: dict[str, int],
    failed_titles: list[str],
    blocked_titles: list[str],
) -> str:
    """Build the deterministic "What happened" sentence for a run's page."""
    if test_run.status == TestRunStatus.WAITING:
        return (
            f"{summary.get('total', 0)} cases selected. This run has not started yet."
        )
    if test_run.status in (*ACTIVE_RUN_STATUSES, TestRunStatus.CANCELLED):
        return _build_narrative_for_active_run(test_run, summary)
    return _build_narrative_for_finished_run(summary, failed_titles, blocked_titles)


# ============================================================================
# BROADCAST HELPERS
# ============================================================================


def _test_run_group(test_run_id: int) -> str:
    return f"test_run_{test_run_id}"


def _test_run_case_group(pivot_id: int) -> str:
    return f"test_run_case_{pivot_id}"


class PivotStatusEvent(TypedDict):
    type: str
    pivot_id: int
    status: str
    summary: dict[str, int]


class CaseStatusEvent(TypedDict):
    type: str
    status: str
    result: str
    blocked_reason: str
    blocked_reason_label: str
    steps: StepProgressPayload


class CaseStepsEvent(TypedDict):
    type: str
    steps: StepProgressPayload


class TestRunStatusEvent(TypedDict):
    type: str
    test_run_status: str
    summary: dict[str, int]
    # A ``RunHelpState`` value.
    help_state: str
    # A ``RunHelpMode`` value; it can change while the run is active.
    help_mode: str


class CaseLogEvent(TypedDict):
    type: str
    message: str


class CaseScreenshotEvent(TypedDict):
    type: str
    screenshot_id: int
    image_url: str
    tool_name: str
    created_at: str


def _broadcast_pivot_status_to_run(pivot: TestRunTestCase) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return
    _send_pivot_status_to_run(layer, pivot, get_test_run_summary(pivot.test_run))


def _send_pivot_status_to_run(
    layer: Any, pivot: TestRunTestCase, summary: dict[str, int]
) -> None:
    event: PivotStatusEvent = {
        "type": "test_run.pivot_status",
        "pivot_id": pivot.id,
        "status": pivot.status,
        "summary": summary,
    }
    async_to_sync(layer.group_send)(_test_run_group(pivot.test_run_id), event)


def broadcast_run_and_pivots(
    test_run: TestRun, pivots: Sequence[TestRunTestCase]
) -> None:
    """Push the run's status and each pivot's status to the run and case pages.

    The run summary is computed once (one query), not once per pivot.
    """
    layer: Any = get_channel_layer()
    if layer is None:
        return
    summary = get_test_run_summary(test_run)
    run_event: TestRunStatusEvent = {
        "type": "test_run.status",
        "test_run_status": test_run.status,
        "summary": summary,
        "help_state": get_run_help_state(test_run),
        "help_mode": test_run.help_mode,
    }
    async_to_sync(layer.group_send)(_test_run_group(test_run.id), run_event)
    for pivot in pivots:
        _send_pivot_status_to_run(layer, pivot, summary)
        _broadcast_pivot_status_to_case(pivot)


def _broadcast_pivot_status_to_case(pivot: TestRunTestCase) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    blocked_reason, blocked_reason_label = get_blocked_reason_fields(pivot)
    event: CaseStatusEvent = {
        "type": "test_run_case.status",
        "status": pivot.status,
        "result": pivot.result,
        "blocked_reason": blocked_reason,
        "blocked_reason_label": blocked_reason_label,
        "steps": pivot_step_payload(pivot),
    }
    async_to_sync(layer.group_send)(_test_run_case_group(pivot.id), event)


def _broadcast_case_steps(
    pivot: TestRunTestCase, checkpoint_data: dict[str, object]
) -> None:
    """Push the running case's step rows after a checkpoint write landed."""
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: CaseStepsEvent = {
        "type": "test_run_case.steps",
        "steps": case_step_payload(
            checkpoint_data, TestRunTestCaseStatus.IN_PROGRESS, pivot.started_at
        ),
    }
    async_to_sync(layer.group_send)(_test_run_case_group(pivot.id), event)


def get_blocked_reason_fields(pivot: TestRunTestCase) -> tuple[str, str]:
    """The case's blocked reason code and label; both empty unless it is blocked."""
    if pivot.status != TestRunTestCaseStatus.BLOCKED or not pivot.blocked_reason:
        return "", ""
    return pivot.blocked_reason, BlockedReason(pivot.blocked_reason).label


def broadcast_test_run_status(test_run: TestRun) -> None:
    """Push the run's status, summary and help state to the run page."""
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: TestRunStatusEvent = {
        "type": "test_run.status",
        "test_run_status": test_run.status,
        "summary": get_test_run_summary(test_run),
        "help_state": get_run_help_state(test_run),
        "help_mode": test_run.help_mode,
    }
    async_to_sync(layer.group_send)(_test_run_group(test_run.id), event)


def _broadcast_log(pivot: TestRunTestCase, message: str) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: CaseLogEvent = {
        "type": "test_run_case.log",
        "message": message,
    }
    async_to_sync(layer.group_send)(_test_run_case_group(pivot.id), event)


def _broadcast_screenshot(
    pivot: TestRunTestCase, screenshot: TestRunScreenshot
) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: CaseScreenshotEvent = {
        "type": "test_run_case.screenshot",
        "screenshot_id": screenshot.id,
        "image_url": screenshot.image.url,
        "tool_name": screenshot.tool_name,
        "created_at": dateformat.format(screenshot.created_at, "M d, H:i"),
    }
    async_to_sync(layer.group_send)(_test_run_case_group(pivot.id), event)


def fetch_test_run_state(
    test_run_id: int,
) -> tuple[TestRun, dict[str, int], list[tuple[int, str]], RunHelpState]:
    test_run = TestRun.objects.get(pk=test_run_id)
    summary = get_test_run_summary(test_run)
    pivot_data: list[tuple[int, str]] = list(
        test_run.pivot_entries.values_list("id", "status")
    )
    return test_run, summary, pivot_data, get_run_help_state(test_run)


def fetch_test_case_state(
    pivot_id: int,
) -> tuple[TestRunTestCase, list[TestRunScreenshot]]:
    pivot = TestRunTestCase.objects.select_related("test_case").get(pk=pivot_id)
    screenshots: list[TestRunScreenshot] = list(
        pivot.screenshots.all().order_by("created_at")
    )
    return pivot, screenshots


# ============================================================================
# TEST RUN EXECUTION SERVICES
# ============================================================================


def _build_cancellation_check(test_run_id: int, generation: int) -> Callable[[], bool]:
    """True once the run is cancelled, or resumed/restarted by a newer chain."""
    stopped = Q(status=TestRunStatus.CANCELLED) | ~Q(execution_generation=generation)

    def _is_cancelled() -> bool:
        return TestRun.objects.filter(stopped, pk=test_run_id).exists()

    return _is_cancelled


def _is_stale_execution(test_run_id: int, generation: int) -> bool:
    """A newer Start or Resume took over the run; this worker must not write."""
    return not TestRun.objects.filter(
        pk=test_run_id, execution_generation=generation
    ).exists()


def execute_test_run_test_case(pivot_id: int, generation: int | None = None) -> None:
    """Run one case as part of the run's ``generation`` (None: the current one)."""
    reset_outage_clock()
    pivot = _fetch_pivot(pivot_id)
    # A paused run sleeps until its Need help case is resolved, which queues
    # the remaining cases in a new chain; this chain's leftovers stand down.
    if pivot.test_run.status in (TestRunStatus.CANCELLED, TestRunStatus.PAUSED):
        return
    if generation is None:
        generation = pivot.test_run.execution_generation
    if pivot.test_run.execution_generation != generation:
        # A task of a chain that was aborted and then resumed or restarted.
        logger.info("Skipping stale task for pivot %d", pivot_id)
        return

    from agents.services.case_teardown import (  # local import: breaks circular dependency
        run_case_teardown,
    )
    from agents.services.orchestrator import (  # local import: breaks circular dependency
        run_orchestrator,
    )

    project = pivot.test_run.project
    run_id = pivot.test_run.id
    cancellation_check = _build_cancellation_check(run_id, generation)
    # A worker shutdown (BaseException) must not start an agent on its way out.
    allow_teardown = True
    # A case paused for help keeps the machine exactly as it is: no teardown,
    # no cleanup, so a person (or the auto-fix agent) sees what went wrong.
    paused_for_help = False
    # Continuing after help: the machine was fixed by hand and must not be
    # cleaned up before the failing step re-runs.
    checkpoint_help = read_checkpoint_help(pivot.execution_checkpoint)
    continue_after_help = (
        checkpoint_help is not None and checkpoint_help.continue_pending
    )
    # A resumed case restores the earlier steps' state before its cursor step
    # (``run_orchestrator``); a fault before that restore finished keeps it
    # pending for Continue. None when no restore is due.
    resume = load_resume_checkpoint(pivot)
    restore_baseline = (
        _restore_attempt_count(resume)
        if resume is not None
        and not continue_after_help
        and 1 < resume.next_step <= len(resume.plan)
        else None
    )
    _mark_pivot_in_progress(pivot)

    try:
        wait_for_controller(project.id)

        wait_for_ready_worker(
            timeout=float(settings.VISION_WORKER_READY_TIMEOUT),
            cancellation_check=cancellation_check,
        )

        if not continue_after_help:
            _safe_cleanup(project.id, wait_for_runner=True)

        on_log = _build_log_callback(pivot, generation)
        on_screenshot = _build_screenshot_callback(pivot, generation)
        task_description = _build_task_description(pivot.test_case)

        project.refresh_from_db(
            fields=["project_prompt", "application_url", "application_platform"]
        )
        # A resumed case keeps its log and continues the persisted plan.
        if resume is not None:
            on_log(f"=== Resumed from step {resume.next_step} ===")
        result = run_orchestrator(
            task_description,
            project.id,
            on_log=on_log,
            on_screenshot=on_screenshot,
            system_info=project.agent_system_info or None,
            project_prompt=build_agent_project_context(project),
            cancellation_check=cancellation_check,
            resume=resume,
            on_checkpoint=build_checkpoint_callback(
                pivot,
                generation,
                on_saved=lambda data: _broadcast_case_steps(pivot, data),
            ),
        )
        # Once a Resume or Start took the run over, the pivot belongs to the
        # newer chain: this worker records nothing (here and below).
        if not _is_stale_execution(run_id, generation):
            if result.stop_reason == AgentStopReason.NEEDS_HUMAN_HELP:
                paused_for_help = _pause_run_for_help(
                    pivot, _help_request_info(pivot, result), generation
                )
            else:
                _finalize_pivot(pivot, result)
                # Only the agent's verdict notifies; the entry point re-checks
                # the generation, since _finalize_pivot holds no run lock.
                if pivot.status == TestRunTestCaseStatus.FAILED:
                    notify_case_failed(pivot.pk, generation=generation)
    except (AgentCancelledError, ControllerNotConnectedError):
        # Inside a case, wait_for_controller only reports "not connected" once
        # the run is no longer STARTED, i.e. it was aborted.
        logger.info("Test run cancelled for pivot %d", pivot_id)
        if not _is_stale_execution(run_id, generation):
            pivot.refresh_from_db()
            if pivot.status != TestRunTestCaseStatus.CANCELLED:
                _mark_pivot_cancelled(pivot)
    except ControllerReconnectTimeoutError as exc:
        # The runner stayed away past the grace period: ask for help instead of
        # cancelling the run, so the remaining cases continue once it is back.
        logger.warning("Runner lost for pivot %d: %s", pivot_id, exc)
        paused_for_help = _pause_case_after_fault(
            pivot, exc, generation, restore_baseline
        )
    except Exception as exc:
        # Infrastructure faults, the soft time limit and bugs are never a
        # verdict on the app: the case asks for help and the run pauses.
        paused_for_help = _pause_case_after_fault(
            pivot, exc, generation, restore_baseline
        )
    except BaseException as exc:
        allow_teardown = False
        logger.critical(
            "execute_test_run_test_case hit BaseException for pivot %d: %s",
            pivot_id,
            exc,
        )
        paused_for_help = _pause_case_after_fault(
            pivot, exc, generation, restore_baseline
        )
        raise
    finally:
        # A newer chain may already be driving the machine: leave it alone.
        # Checked again after the teardown, which can take minutes.
        if (
            allow_teardown
            and not paused_for_help
            and not _is_stale_execution(run_id, generation)
        ):
            _safe_teardown(
                pivot,
                project,
                generation=generation,
                cancellation_check=cancellation_check,
                run_case_teardown=run_case_teardown,
            )
        if not _is_stale_execution(run_id, generation):
            if not paused_for_help:
                _safe_cleanup(project.id, wait_for_runner=False)
            _update_test_run_status_if_needed(pivot.test_run)


def _restore_attempt_count(checkpoint: OrchestratorCheckpoint) -> int:
    return sum(1 for attempt in checkpoint.attempts if attempt.kind == "resume")


def _pause_case_after_fault(
    pivot: TestRunTestCase,
    exc: BaseException,
    generation: int,
    restore_baseline: int | None,
) -> bool:
    """Ask for help after a fault stopped a case with no verdict yet.

    Only a person marks a case Blocked. A case that already left In progress
    (the run's abort settled it) is left alone, a run cancelled in the
    meantime cancels the case, and a stale generation stands down; the last
    two are checked again under the run lock by ``_pause_run_for_help``.
    ``restore_baseline`` is the number of restore attempts when the case
    started with a restore due (None otherwise): no new one means the restore
    never finished, so Continue restores. True when the case paused (no
    teardown and no cleanup follow).
    """
    if _is_stale_execution(pivot.test_run_id, generation):
        return False
    pivot.refresh_from_db(fields=["status"])
    if pivot.status != TestRunTestCaseStatus.IN_PROGRESS:
        logger.warning(
            "Execution error for pivot %d after it reached %s: %r",
            pivot.id,
            pivot.status,
            exc,
        )
        return False

    test_run = pivot.test_run
    test_run.refresh_from_db(fields=["status"])
    if test_run.status == TestRunStatus.CANCELLED:
        logger.info("Test run cancelled for pivot %d (%r)", pivot.id, exc)
        _mark_pivot_cancelled(pivot)
        return False

    reason = classify_case_fault(exc)
    if reason == BlockedReason.INTERNAL_ERROR:
        logger.exception("Pivot %d needs help after an unexpected error", pivot.id)
    else:
        logger.warning("Pivot %d needs help (%s): %s", pivot.id, reason.value, exc)
    # The checkpoint callback keeps the in-memory checkpoint current.
    checkpoint = load_resume_checkpoint(pivot)
    # A fault during the verdict leaves the cursor past the last step; the
    # request names the last step, and Continue still goes on to the verdict.
    step_num = (
        min(checkpoint.next_step, max(len(checkpoint.plan), 1))
        if checkpoint is not None
        else 1
    )
    help = HelpRequestInfo(
        step_num=step_num,
        reason=fault_message(exc, reason),
        ask=fault_ask(reason),
    )
    return _pause_run_for_help(
        pivot,
        help,
        generation,
        fault=reason,
        continue_mode=help_continue_mode(
            reason,
            checkpoint,
            restore_pending=(
                restore_baseline is not None
                and checkpoint is not None
                and _restore_attempt_count(checkpoint) == restore_baseline
            ),
        ),
    )


def _fetch_pivot(pivot_id: int) -> TestRunTestCase:
    return TestRunTestCase.objects.select_related("test_run__project", "test_case").get(
        pk=pivot_id
    )


def _mark_pivot_in_progress(pivot: TestRunTestCase) -> None:
    pivot.status = TestRunTestCaseStatus.IN_PROGRESS
    pivot.started_at = timezone.now()
    pivot.save(update_fields=["status", "started_at", "updated_at"])

    test_run = pivot.test_run
    status_changed = False
    if test_run.status == TestRunStatus.WAITING:
        test_run.status = TestRunStatus.STARTED
        test_run.started_at = timezone.now()
        test_run.save(update_fields=["status", "started_at", "updated_at"])
        status_changed = True

    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)
    if status_changed:
        broadcast_test_run_status(test_run)


def _build_log_callback(
    pivot: TestRunTestCase, generation: int
) -> Callable[[str], None]:
    def _append_log(message: str) -> None:
        if _append_pivot_log(pivot.pk, message, generation):
            _broadcast_log(pivot, message)

    return _append_log


def _append_pivot_log(pivot_id: int, line: str, generation: int | None) -> bool:
    """Append one line in the database (never rewrite the whole log).

    With a ``generation`` the line only lands while the run still has it, so a
    worker a Resume took over cannot interleave with the resumed case's log.
    """
    rows = TestRunTestCase.objects.filter(pk=pivot_id)
    if generation is not None:
        rows = rows.filter(test_run__execution_generation=generation)
    return (
        rows.update(
            logs=Concat(F("logs"), Value(line + "\n")), updated_at=timezone.now()
        )
        > 0
    )


def _build_screenshot_callback(
    pivot: TestRunTestCase, generation: int
) -> ScreenshotCallback:
    def _save_screenshot(base64_data: str, tool_name: str) -> None:
        if _is_stale_execution(pivot.test_run_id, generation):
            return
        image_bytes = _decode_base64_image(base64_data)
        filename = _generate_screenshot_filename(tool_name)
        _persist_screenshot(pivot, image_bytes, filename, tool_name)

    return _save_screenshot


def _decode_base64_image(base64_data: str) -> bytes:
    return base64.b64decode(base64_data)


def _generate_screenshot_filename(tool_name: str) -> str:
    timestamp_ms = int(time.time() * 1000)
    return f"{timestamp_ms}_{tool_name}.png"


def _persist_screenshot(
    pivot: TestRunTestCase,
    image_bytes: bytes,
    filename: str,
    tool_name: str,
) -> None:
    screenshot = TestRunScreenshot.objects.create(
        test_run_test_case=pivot,
        image=ContentFile(image_bytes, name=filename),
        tool_name=tool_name,
    )
    _broadcast_screenshot(pivot, screenshot)


def _build_task_description(test_case: TestCase) -> str:
    parts: list[str] = [f"Test Case: {test_case.title}"]
    if test_case.preconditions:
        parts.append(f"\nPreconditions:\n{test_case.preconditions}")
    if test_case.steps:
        parts.append(f"\nSteps:\n{test_case.steps}")
    if test_case.expected:
        parts.append(f"\nExpected Result:\n{test_case.expected}")
    return "\n".join(parts)


def _finalize_pivot(pivot: TestRunTestCase, result: AgentResult) -> None:
    if result.stop_reason == AgentStopReason.CANCELLED:
        pivot.status = TestRunTestCaseStatus.CANCELLED
    elif result.stop_reason == AgentStopReason.TASK_COMPLETE:
        pivot.status = TestRunTestCaseStatus.SUCCESS
    else:
        pivot.status = TestRunTestCaseStatus.FAILED

    pivot.result = _extract_agent_summary(result)
    pivot.finished_at = timezone.now()
    pivot.save(update_fields=["status", "result", "finished_at", "updated_at"])
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)


_HELP_FALLBACK_ASK = "Check the test machine, then continue the case."


def _help_request_info(pivot: TestRunTestCase, result: AgentResult) -> HelpRequestInfo:
    """The orchestrator's help request; rebuilt from the checkpoint if missing."""
    if result.help is not None:
        return result.help
    logger.warning("Pivot %d needs help without a help request", pivot.pk)
    checkpoint = load_resume_checkpoint(pivot)
    step_num = checkpoint.next_step if checkpoint is not None else 1
    return HelpRequestInfo(
        step_num=step_num,
        reason=_extract_agent_summary(result),
        ask=_HELP_FALLBACK_ASK,
    )


def _truncate_help_text(text: str) -> str:
    text = text.strip()
    if len(text) <= HELP_REQUEST_TEXT_MAX_LENGTH:
        return text
    return text[: HELP_REQUEST_TEXT_MAX_LENGTH - 1].rstrip() + "…"


def _pause_run_for_help(
    pivot: TestRunTestCase,
    help: HelpRequestInfo,
    generation: int,
    *,
    fault: BlockedReason | None = None,
    continue_mode: HelpContinueMode = HelpContinueMode.CURRENT_SCREEN,
) -> bool:
    """Put the case in Need help and the run to sleep; True when it did.

    ``fault`` is set when an infrastructure fault (not the orchestrator)
    stopped the case; the Night auto-fix decision is the same either way.
    ``continue_mode`` is stored on the request and drives Continue.

    One transaction under the run lock: a stale generation does nothing, and
    a run aborted meanwhile cancels the case instead. The case keeps its
    checkpoint and the machine is left as it is. After commit the run page is
    told, then either the Night auto-fix is enqueued (by task name: importing
    ``machine_setup.tasks`` here would be an import cycle) or the project
    members are notified.
    """
    reason = _truncate_help_text(help.reason)
    ask = _truncate_help_text(help.ask) or _HELP_FALLBACK_ASK
    step_num = max(help.step_num, 1)
    with transaction.atomic():
        test_run = TestRun.objects.select_for_update().get(pk=pivot.test_run_id)
        if test_run.execution_generation != generation:
            return False
        pivot.refresh_from_db(fields=["status"])
        if test_run.status != TestRunStatus.STARTED:
            if pivot.status == TestRunTestCaseStatus.IN_PROGRESS:
                _mark_pivot_cancelled(pivot)
            return False
        if pivot.status != TestRunTestCaseStatus.IN_PROGRESS:
            return False

        auto_fix = test_run.help_mode == RunHelpMode.NIGHT and auto_fix_eligible(
            pivot.pk, step_num
        )
        now = timezone.now()
        # At most one open request per case: close a leftover before opening.
        CaseHelpRequest.objects.filter(
            test_run_test_case_id=pivot.pk, resolution=HelpResolution.OPEN
        ).update(resolution=HelpResolution.CANCELLED, resolved_at=now)
        help_request = CaseHelpRequest(
            test_run_test_case=pivot,
            step_num=step_num,
            reason=reason,
            ask=ask,
            fault=fault or "",
            continue_mode=continue_mode,
        )
        if auto_fix:
            mark_auto_fix_running(help_request)
        help_request.save()

        pivot.status = TestRunTestCaseStatus.NEED_HELP
        pivot.blocked_reason = ""
        pivot.result = ask
        pivot.finished_at = None
        pivot.save(
            update_fields=[
                "status",
                "blocked_reason",
                "result",
                "finished_at",
                "updated_at",
            ]
        )
        test_run.status = TestRunStatus.PAUSED
        test_run.save(update_fields=["status", "updated_at"])

        if fault is None:
            log_line = f"[Need help at step {step_num}] {ask}"
        else:
            log_line = f"[Need help: {fault.label} at step {step_num}] {ask}"
        if auto_fix:
            log_line += " The machine setup agent will try to fix it."
        logged = _append_pivot_log(pivot.pk, log_line, generation)
        help_request_id = help_request.pk

        def _after_commit() -> None:
            if logged:
                _broadcast_log(pivot, log_line)
            broadcast_run_and_pivots(test_run, [pivot])
            if auto_fix:
                enqueue_case_auto_fix(help_request_id)
            else:
                notify_case_needs_help(help_request_id)

        transaction.on_commit(_after_commit)
    return True


def _extract_agent_summary(result: AgentResult) -> str:
    for message in reversed(result.messages):
        if message.role == "assistant" and isinstance(message.content, str):
            return message.content
    return result.error or ""


def _mark_pivot_cancelled(pivot: TestRunTestCase) -> None:
    pivot.status = TestRunTestCaseStatus.CANCELLED
    pivot.finished_at = timezone.now()
    pivot.save(update_fields=["status", "finished_at", "updated_at"])
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)


@transaction.atomic
def abort_test_run(
    test_run: TestRun, reason: str = "Test run aborted", *, revoke_task: bool = True
) -> None:
    # Under the row lock: a concurrent Resume may have changed the status.
    locked_status = (
        TestRun.objects.select_for_update()
        .values_list("status", flat=True)
        .get(pk=test_run.pk)
    )
    if locked_status not in ACTIVE_RUN_STATUSES:
        return

    now = timezone.now()
    test_run.status = TestRunStatus.CANCELLED
    test_run.finished_at = now
    test_run.save(update_fields=["status", "finished_at", "updated_at"])

    # Help requests before cases: the lock order of every help writer.
    _cancel_run_help_requests(test_run, now)

    # A Need help case is Cancelled like a running one: Resume continues it.
    for pivot in test_run.pivot_entries.filter(
        status__in=[
            TestRunTestCaseStatus.IN_PROGRESS,
            TestRunTestCaseStatus.NEED_HELP,
        ]
    ):
        _mark_pivot_cancelled(pivot)

    for pivot in test_run.pivot_entries.filter(status=TestRunTestCaseStatus.CREATED):
        _mark_pivot_cancelled(pivot)

    broadcast_test_run_status(test_run)

    celery_task_id = test_run.celery_task_id
    # A paused run has no worker executing it: nothing to revoke.
    if locked_status == TestRunStatus.STARTED and revoke_task and celery_task_id:
        transaction.on_commit(
            lambda: celery_app.control.revoke(celery_task_id, terminate=True)
        )


def _cancel_run_help_requests(test_run: TestRun, now: datetime) -> None:
    """Close the run's open help request and stop its running auto-fix.

    Its pending redesign drafts are superseded (step lists cleared, as
    ``case_plan_revision_services.supersede_pending_revisions`` does) and the
    help-chat agent's suggestion is cleared, as every resolution does. Done
    with direct updates: that module imports this one. The auto-fix turn honours the stop at its next tool boundary; its late
    settle only records the outcome on the cancelled run. An auto-fix with no
    live turn (its start task is still queued, or was lost) is settled not
    fixed here, since no turn will ever settle it; the late start task is a
    no-op on the cancelled run.
    """
    requests = CaseHelpRequest.objects.filter(test_run_test_case__test_run=test_run)
    open_requests = requests.filter(resolution=HelpResolution.OPEN)
    CasePlanRevision.objects.filter(
        help_request__in=open_requests, status=PlanRevisionStatus.PROPOSED
    ).update(
        status=PlanRevisionStatus.SUPERSEDED,
        decided_at=now,
        steps=[],
        previous_steps=[],
    )
    open_requests.update(
        resolution=HelpResolution.CANCELLED,
        resolved_at=now,
        agent_proposal_action=HelpProposalAction.NONE,
        agent_proposal_note="",
        agent_proposal_at=None,
    )
    auto_fixing_ids = list(
        requests.filter(auto_fix_state=HelpAutoFixState.RUNNING).values_list(
            "id", flat=True
        )
    )
    turnless_ids = [
        help_request_id
        for help_request_id in auto_fixing_ids
        if not request_auto_fix_stop(help_request_id)
    ]
    if turnless_ids:
        CaseHelpRequest.objects.filter(
            pk__in=turnless_ids, auto_fix_state=HelpAutoFixState.RUNNING
        ).update(auto_fix_state=HelpAutoFixState.NOT_FIXED)


def abort_active_test_run_on_disconnect(project: Project) -> None:
    # STARTED only: a paused run executes nothing, so a runner drop while a
    # person fixes the machine (or the auto-fix restarts it) must not cancel it.
    active_run = TestRun.objects.filter(
        project=project, status=TestRunStatus.STARTED
    ).first()
    if active_run is not None:
        abort_test_run(active_run, reason="Controller client disconnected")


def _update_test_run_status_if_needed(test_run: TestRun) -> None:
    test_run.refresh_from_db()
    # A paused run finishes only after its Need help case is resolved.
    if test_run.status in (TestRunStatus.CANCELLED, TestRunStatus.PAUSED):
        return

    all_pivots = test_run.pivot_entries.all()

    if not all_pivots.exists():
        return

    all_done = not all_pivots.filter(
        status__in=[
            TestRunTestCaseStatus.CREATED,
            TestRunTestCaseStatus.IN_PROGRESS,
            TestRunTestCaseStatus.NEED_HELP,
        ]
    ).exists()

    if all_done:
        test_run.status = TestRunStatus.DONE
        test_run.finished_at = timezone.now()
        test_run.save(update_fields=["status", "finished_at", "updated_at"])
        broadcast_test_run_status(test_run)


# ============================================================================
# CONTROLLER CLIENT DOWNLOAD SERVICES
# ============================================================================

_CONTROLLER_CLIENT_EXCLUDE_DIRS = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "tests",
    "weights",
}
_CONTROLLER_CLIENT_EXCLUDE_FILES = {".env"}


def _should_include_path(relative_path: Path) -> bool:
    for part in relative_path.parts:
        if part in _CONTROLLER_CLIENT_EXCLUDE_DIRS:
            return False
    if relative_path.name in _CONTROLLER_CLIENT_EXCLUDE_FILES:
        return False
    return True


def _generate_env_content(project: Project) -> str:
    return (
        f"CONTROLLER_HOST={settings.CONTROLLER_SERVER_HOST}\n"
        f"CONTROLLER_PORT={settings.CONTROLLER_SERVER_PORT}\n"
        f"CONTROLLER_API_KEY={project.api_key}\n"
        "CONTROLLER_RECONNECT_INTERVAL=5\n"
        "CONTROLLER_MAX_RECONNECT_ATTEMPTS=0\n"
        "CONTROLLER_RECONNECT_MAX_INTERVAL=60\n"
        "CONTROLLER_LOG_LEVEL=INFO\n"
    )


def generate_controller_client_zip(project: Project) -> bytes:
    controller_dir = settings.BASE_DIR / "controller_client"
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(controller_dir.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(controller_dir)
            if not _should_include_path(relative):
                continue
            arcname = str(Path("controller_client") / relative)
            zf.write(file_path, arcname)

        env_content = _generate_env_content(project)
        zf.writestr("controller_client/.env", env_content)

    return buffer.getvalue()
