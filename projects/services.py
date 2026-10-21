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
from typing import Any, NoReturn, TypedDict, TypeVar

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
    When,
)
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import dateformat, timezone

from accounts.models import CustomUser
from agents.types import (
    AgentCancelledError,
    AgentResult,
    AgentStopReason,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
    ScreenshotCallback,
)
from auto_tester.celery import app as celery_app
from controller_client.protocol import (
    CLIENT_VERSION,
    REQUIRED_CLIENT_CAPABILITIES,
    ErrorCode,
    OmniParserState,
    OmniParserStatusPayload,
    parse_omniparser_status_payload,
)
from projects.case_blocking import blocked_message, classify_case_exception
from projects.controller_outage_clock import agent_monotonic, reset_outage_clock
from projects.controller_reconnect_services import (
    CONTROLLER_POLL_INTERVAL_SECONDS,
    connected_owner,
    controller_connection_lost,
    wait_for_controller,
)
from projects.exceptions import (
    ControllerActionError,
    ControllerNotConnectedError,
    ControllerReconnectTimeoutError,
    TestRunnerDisconnectedError,
    TestRunnerUnresponsiveError,
    VisualEngineError,
)
from projects.models import (
    ApplicationPlatform,
    BlockedReason,
    Project,
    Tag,
    TestCase,
    TestCaseData,
    TestCaseUpload,
    TestRailPushStatus,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
    UploadStatus,
)

logger = logging.getLogger(__name__)

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
    cases = project.test_cases.order_by("title")
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


def get_machine_state(project: Project) -> MachineState:
    """Derive one machine badge state from `agent_connected` and OmniParser status.

    A freshly connected controller reports an empty OmniParser status
    dict until its first status update arrives, which is treated as
    STARTING rather than FAILED.
    """
    if not project.agent_connected:
        return MachineState.DISCONNECTED
    if not project.agent_omniparser_status:
        return MachineState.STARTING

    status = parse_omniparser_status_payload(project.agent_omniparser_status)
    if status.state == OmniParserState.READY:
        return MachineState.READY
    if status.state == OmniParserState.FAILED:
        return MachineState.FAILED
    return MachineState.STARTING


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


def get_project_overview(project: Project) -> ProjectOverview:
    """Gather the project overview page's data in a fixed, small number of queries."""
    case_count = project.test_cases.count()
    never_run_count = project.test_cases.filter(run_entries__isnull=True).count()
    machine_state = get_machine_state(project)
    active_run = TestRun.objects.filter(
        project=project, status=TestRunStatus.STARTED
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


def _visual_engine_ready_check(machine_state: MachineState) -> ReadinessCheck:
    passed = machine_state == MachineState.READY
    if passed:
        title = "Visual engine ready"
        help_text = ""
    else:
        title = "Visual engine not ready"
        help_text = (
            "The Test Runner is still starting its visual engine — this "
            "usually takes a few minutes after it connects."
        )
    return ReadinessCheck(
        key="visual_engine_ready",
        passed=passed,
        blocking=True,
        title=title,
        help=help_text,
        link_url=None,
    )


def _no_active_run_check(project: Project, test_run: TestRun | None) -> ReadinessCheck:
    active_runs = TestRun.objects.filter(project=project, status=TestRunStatus.STARTED)
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


def get_run_readiness(
    project: Project,
    *,
    test_run: TestRun | None = None,
    selected_count: int | None = None,
) -> RunReadiness:
    """Compute the "Ready to run?" checklist shown before starting a run."""
    machine_state = get_machine_state(project)
    checks = (
        _cases_selected_check(test_run, selected_count),
        _machine_connected_check(project, machine_state),
        _visual_engine_ready_check(machine_state),
        _no_active_run_check(project, test_run),
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


def save_project_prompt(*, project: Project, prompt: str) -> Project:
    project.project_prompt = prompt
    project.save(update_fields=["project_prompt", "updated_at"])
    return project


def save_application_context(
    *,
    project: Project,
    application_url: str,
    application_platform: str,
    project_prompt: str,
) -> Project:
    """Persist the application context used to brief the AI agent."""
    project.application_url = application_url
    project.application_platform = application_platform
    project.project_prompt = project_prompt
    project.save(
        update_fields=[
            "application_url",
            "application_platform",
            "project_prompt",
            "updated_at",
        ]
    )
    return project


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


def _initial_omniparser_status() -> dict[str, Any]:
    return _serialize_omniparser_status(
        OmniParserStatusPayload(
            state=OmniParserState.LOADING,
            message="Waiting for the controller to report OmniParser readiness",
            device="",
            weights_dir="",
            phase="handshake",
            load_seconds=0.0,
        )
    )


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
            agent_omniparser_status=_initial_omniparser_status(),
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
    agent_omniparser_status: dict[str, object]
    last_connected_at: datetime


def _disconnected_agent_fields() -> _DisconnectedAgentFields:
    return {
        "agent_connected": False,
        "agent_channel_name": "",
        "agent_system_info": {},
        "agent_client_version": "",
        "agent_capabilities": [],
        "agent_omniparser_status": {},
        "last_connected_at": timezone.now(),
    }


def update_agent_omniparser_status(
    project: Project, status: OmniParserStatusPayload
) -> None:
    """Persist the controller's latest OmniParser readiness report."""
    project.agent_omniparser_status = _serialize_omniparser_status(status)
    project.save(update_fields=["agent_omniparser_status"])
    broadcast_agent_status(project)


def _serialize_omniparser_status(status: OmniParserStatusPayload) -> dict[str, Any]:
    return {
        "state": status.state.value,
        "message": status.message,
        "device": status.device,
        "weights_dir": status.weights_dir,
        "phase": status.phase,
        "load_seconds": status.load_seconds,
    }


class AgentStatusEvent(TypedDict):
    type: str
    agent_connected: bool
    agent_system_info: dict[str, Any]
    agent_client_version: str
    agent_capabilities: list[str]
    agent_omniparser_status: dict[str, Any]
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
        "agent_omniparser_status": project.agent_omniparser_status,
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
# Error codes meaning the visual engine (OmniParser), not the app, failed.
_VISUAL_ENGINE_ERROR_CODES = frozenset(
    {ErrorCode.FIND_ELEMENT_FAILED, ErrorCode.OMNIPARSER_NOT_READY}
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
    if code in _VISUAL_ENGINE_ERROR_CODES:
        raise VisualEngineError(message)
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
    application under test (input, screenshot, find_element, cleanup, ...):
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


def controller_screenshot(
    project_id: int,
    timeout: float = 30.0,
) -> ScreenshotResult:
    reply = _dispatch_controller_action(
        project_id, "controller.screenshot", timeout, runner_local=True
    )
    return _build_screenshot_result(reply, "take a screenshot")


def _build_pixel_element(data: dict[str, Any]) -> PixelUIElement:
    bbox_data: dict[str, Any] = data.get("bbox", {})
    return PixelUIElement(
        index=data.get("index", 0),
        type=data.get("type", "unknown"),
        content=data.get("content", ""),
        bbox=PixelBBox(
            x_min=bbox_data.get("x_min", 0),
            y_min=bbox_data.get("y_min", 0),
            x_max=bbox_data.get("x_max", 0),
            y_max=bbox_data.get("y_max", 0),
        ),
        center_x=data.get("center_x", 0),
        center_y=data.get("center_y", 0),
        interactivity=data.get("interactivity", False),
    )


def _raise_for_find_element_failure(reply: dict[str, Any]) -> None:
    if reply.get("success", False):
        return

    message = reply.get("message") or "Controller failed to find elements on screen"
    details = reply.get("details", "")
    if details:
        message = f"{message} ({details})"
    raise VisualEngineError(message)


def controller_find_elements(
    project_id: int,
    box_threshold: float | None = None,
    iou_threshold: float | None = None,
    timeout: float | None = None,
) -> PixelParseResult:
    resolved_timeout = (
        float(settings.CONTROLLER_FIND_ELEMENT_TIMEOUT) if timeout is None else timeout
    )
    reply = _dispatch_controller_action(
        project_id,
        "controller.find_element",
        resolved_timeout,
        runner_local=True,
        box_threshold=box_threshold,
        iou_threshold=iou_threshold,
    )
    _raise_for_find_element_failure(reply)

    elements_data: list[dict[str, Any]] = reply.get("elements", [])
    elements = tuple(_build_pixel_element(el) for el in elements_data)

    return PixelParseResult(
        annotated_image=reply.get("annotated_image_base64", ""),
        elements=elements,
        image_width=reply.get("image_width", 0),
        image_height=reply.get("image_height", 0),
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
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_navigate", timeout, url=url
    )
    return _build_action_result(reply)


def controller_browser_click(
    project_id: int,
    element_index: int,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_click",
        timeout,
        element_index=element_index,
    )
    return _build_action_result(reply)


def controller_browser_type(
    project_id: int,
    element_index: int,
    text: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_type",
        timeout,
        element_index=element_index,
        text=text,
    )
    return _build_action_result(reply)


def controller_browser_hover(
    project_id: int,
    element_index: int,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_hover",
        timeout,
        element_index=element_index,
    )
    return _build_action_result(reply)


def controller_browser_get_elements(
    project_id: int,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_get_elements", timeout
    )
    return _build_browser_content_result(reply)


def controller_browser_get_page_content(
    project_id: int,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_get_page_content", timeout
    )
    return _build_browser_content_result(reply)


def controller_browser_get_url(
    project_id: int,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_get_url", timeout
    )
    return _build_browser_content_result(reply)


def controller_browser_download(
    project_id: int,
    url: str,
    save_path: str = "",
    timeout: float = 120.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_download", timeout, url=url, save_path=save_path
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


def controller_browser_take_screenshot(
    project_id: int,
    timeout: float = 30.0,
) -> ScreenshotResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_take_screenshot", timeout
    )
    return _build_screenshot_result(reply, "take a browser screenshot")


# ============================================================================
# TEST CASE SERVICES
# ============================================================================


def create_test_case(*, project: Project, data: TestCaseData) -> TestCase:
    return TestCase.objects.create(project=project, **asdict(data))


def can_edit_test_case_in_run(test_run: TestRun) -> bool:
    return test_run.status != TestRunStatus.STARTED


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
    valid_cases = TestCase.objects.filter(id__in=test_case_ids, project=project)
    pivots = [TestRunTestCase(test_run=test_run, test_case=tc) for tc in valid_cases]
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
        TestRunTestCase(test_run=test_run, test_case=tc)
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

    Uses a stable `created_at, id` ordering rather than the failures-first
    ordering `list_test_run_cases` uses for display, since that ordering
    is not a stable position for a given pivot.
    """
    earlier_count = pivot.test_run.pivot_entries.filter(
        Q(created_at__lt=pivot.created_at)
        | Q(created_at=pivot.created_at, id__lt=pivot.id)
    ).count()
    total = pivot.test_run.pivot_entries.count()
    return earlier_count + 1, total


def get_test_run_case_detail(pivot_id: int, project: Project) -> TestRunTestCase | None:
    try:
        return (
            TestRunTestCase.objects.select_related("test_run", "test_case")
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
    """List a run's cases: failed, then blocked, then the rest; newest first within."""
    qs = (
        test_run.pivot_entries.select_related("test_case")
        .annotate(
            _failure_priority=Case(
                When(status=TestRunTestCaseStatus.FAILED, then=0),
                When(status=TestRunTestCaseStatus.BLOCKED, then=1),
                default=2,
                output_field=IntegerField(),
            )
        )
        .order_by("_failure_priority", "-created_at")
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
    if test_run.status not in (TestRunStatus.DONE, TestRunStatus.CANCELLED):
        raise ValueError("Only completed or cancelled test runs can be reset.")
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
        pivot.save(
            update_fields=[
                "status",
                "blocked_reason",
                "result",
                "logs",
                "started_at",
                "finished_at",
                "updated_at",
            ]
        )
    test_run.status = TestRunStatus.WAITING
    test_run.celery_task_id = ""
    test_run.started_at = None
    test_run.finished_at = None
    test_run.testrail_push_status = TestRailPushStatus.NOT_PUSHED
    test_run.testrail_pushed_at = None
    test_run.testrail_push_error = ""
    test_run.testrail_push_summary = ""
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
            "updated_at",
        ]
    )


def _has_active_test_run(project: Project) -> bool:
    return TestRun.objects.filter(
        project=project, status=TestRunStatus.STARTED
    ).exists()


def start_test_run(test_run: TestRun) -> None:
    from projects.tasks import execute_test_run_case

    pivot_ids = list(test_run.pivot_entries.values_list("id", flat=True))
    if not pivot_ids:
        raise ValueError("Cannot start a test run with no test cases.")
    if test_run.status != TestRunStatus.WAITING:
        raise ValueError("Test run is not in WAITING status.")
    if _has_active_test_run(test_run.project):
        raise ValueError("Another test run is already in progress for this project.")

    test_run.status = TestRunStatus.STARTED
    test_run.started_at = timezone.now()
    test_run.finished_at = None
    test_run.save(update_fields=["status", "started_at", "finished_at", "updated_at"])
    _broadcast_test_run_status(test_run)

    result = chain(*[execute_test_run_case.si(pid) for pid in pivot_ids]).apply_async()
    test_run.celery_task_id = result.id
    test_run.save(update_fields=["celery_task_id", "updated_at"])


def get_test_run_summary(test_run: TestRun) -> dict[str, int]:
    result = test_run.pivot_entries.aggregate(
        total=Count("id"),
        created=Count("id", filter=Q(status=TestRunTestCaseStatus.CREATED)),
        in_progress=Count("id", filter=Q(status=TestRunTestCaseStatus.IN_PROGRESS)),
        success=Count("id", filter=Q(status=TestRunTestCaseStatus.SUCCESS)),
        failed=Count("id", filter=Q(status=TestRunTestCaseStatus.FAILED)),
        blocked=Count("id", filter=Q(status=TestRunTestCaseStatus.BLOCKED)),
        cancelled=Count("id", filter=Q(status=TestRunTestCaseStatus.CANCELLED)),
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
    run adds its verdicts to the same TestRail run.
    """
    case_ids = list(
        test_run.pivot_entries.filter(status=_RERUN_SCOPE_STATUS[scope]).values_list(
            "test_case_id", flat=True
        )
    )
    if not can_rerun_cases(test_run, len(case_ids)):
        raise ValueError(
            f"Only completed or cancelled runs with {scope.value} cases can be rerun."
        )

    new_run = TestRun.objects.create(
        project=test_run.project,
        name=f"{test_run.display_name} — {scope.value} rerun",
        testrail_project_id=test_run.testrail_project_id,
        testrail_suite_id=test_run.testrail_suite_id,
        testrail_run_id=test_run.testrail_run_id,
    )
    pivots = [
        TestRunTestCase(test_run=new_run, test_case_id=case_id) for case_id in case_ids
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
        .order_by("id")
        .values_list("test_case__title", flat=True)[:NARRATIVE_TITLE_LIMIT]
    )


def list_blocked_case_titles(test_run: TestRun) -> list[str]:
    """First blocked case titles of a run, for the "What happened" summary."""
    return list(
        test_run.pivot_entries.filter(status=TestRunTestCaseStatus.BLOCKED)
        .order_by("id")
        .values_list("test_case__title", flat=True)[:NARRATIVE_TITLE_LIMIT]
    )


def _build_narrative_for_active_run(test_run: TestRun, summary: dict[str, int]) -> str:
    total = summary.get("total", 0)
    finished = (
        summary.get("success", 0) + summary.get("failed", 0) + summary.get("blocked", 0)
    )
    if test_run.status == TestRunStatus.CANCELLED:
        return f"Run cancelled after {finished} of {total} cases finished."
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
    if test_run.status in (TestRunStatus.STARTED, TestRunStatus.CANCELLED):
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


class TestRunStatusEvent(TypedDict):
    type: str
    test_run_status: str
    summary: dict[str, int]


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

    event: PivotStatusEvent = {
        "type": "test_run.pivot_status",
        "pivot_id": pivot.id,
        "status": pivot.status,
        "summary": get_test_run_summary(pivot.test_run),
    }
    async_to_sync(layer.group_send)(_test_run_group(pivot.test_run.id), event)


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
    }
    async_to_sync(layer.group_send)(_test_run_case_group(pivot.id), event)


def get_blocked_reason_fields(pivot: TestRunTestCase) -> tuple[str, str]:
    """The case's blocked reason code and label; both empty unless it is blocked."""
    if pivot.status != TestRunTestCaseStatus.BLOCKED or not pivot.blocked_reason:
        return "", ""
    return pivot.blocked_reason, BlockedReason(pivot.blocked_reason).label


def _broadcast_test_run_status(test_run: TestRun) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: TestRunStatusEvent = {
        "type": "test_run.status",
        "test_run_status": test_run.status,
        "summary": get_test_run_summary(test_run),
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
) -> tuple[TestRun, dict[str, int], list[tuple[int, str]]]:
    test_run = TestRun.objects.get(pk=test_run_id)
    summary = get_test_run_summary(test_run)
    pivot_data: list[tuple[int, str]] = list(
        test_run.pivot_entries.values_list("id", "status")
    )
    return test_run, summary, pivot_data


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


def _build_cancellation_check(test_run_id: int) -> Callable[[], bool]:
    def _is_cancelled() -> bool:
        return TestRun.objects.filter(
            pk=test_run_id, status=TestRunStatus.CANCELLED
        ).exists()

    return _is_cancelled


def _wait_for_omniparser_ready(
    project: Project,
    cancellation_check: Callable[[], bool] | None = None,
) -> None:
    """Block until the controller reports OmniParser as ready to use.

    A freshly connected controller still needs to load its OmniParser
    weights in the background; running an action before that finishes
    would otherwise time out with a confusing error. A disconnect waits for
    the Test Runner to reconnect; that wait does not count against the timeout.
    """
    timeout = settings.CONTROLLER_OMNIPARSER_READY_TIMEOUT
    start = agent_monotonic()
    last_state = ""
    while agent_monotonic() - start < timeout:
        _raise_if_run_cancelled(cancellation_check)
        project.refresh_from_db()
        if not project.agent_connected:
            wait_for_controller(project.id)
            continue
        last_state = _raise_for_omniparser_failure(project)
        if last_state == OmniParserState.READY:
            return
        time.sleep(CONTROLLER_POLL_INTERVAL_SECONDS)
    raise VisualEngineError(
        f"OmniParser did not become ready within {timeout}s for project "
        f"{project.id} (last observed state: {last_state or 'unknown'})"
    )


def _raise_if_run_cancelled(cancellation_check: Callable[[], bool] | None) -> None:
    if cancellation_check is not None and cancellation_check():
        raise AgentCancelledError(
            "Test run cancelled while waiting for OmniParser readiness"
        )


def _raise_for_omniparser_failure(project: Project) -> str:
    if not project.agent_omniparser_status:
        return ""
    status = parse_omniparser_status_payload(project.agent_omniparser_status)
    if status.state is not OmniParserState.FAILED:
        return status.state.value

    raise VisualEngineError(
        "OmniParser failed to initialize on the controller: "
        f"{status.message} (phase={status.phase}, device={status.device}, "
        f"weights_dir={status.weights_dir})"
    )


def execute_test_run_test_case(pivot_id: int) -> None:
    reset_outage_clock()
    pivot = _fetch_pivot(pivot_id)
    if pivot.test_run.status == TestRunStatus.CANCELLED:
        return

    project = pivot.test_run.project
    cancellation_check = _build_cancellation_check(pivot.test_run.id)
    _mark_pivot_in_progress(pivot)

    try:
        wait_for_controller(project.id)

        _wait_for_omniparser_ready(project, cancellation_check=cancellation_check)

        _safe_cleanup(project.id, wait_for_runner=True)

        on_log = _build_log_callback(pivot)
        on_screenshot = _build_screenshot_callback(pivot)
        task_description = _build_task_description(pivot.test_case)

        from agents.services.orchestrator import (  # local import: breaks circular dependency
            run_orchestrator,
        )

        project.refresh_from_db(
            fields=["project_prompt", "application_url", "application_platform"]
        )
        result = run_orchestrator(
            task_description,
            project.id,
            on_log=on_log,
            on_screenshot=on_screenshot,
            system_info=project.agent_system_info or None,
            project_prompt=build_agent_project_context(project),
            cancellation_check=cancellation_check,
        )
        _finalize_pivot(pivot, result)
    except (AgentCancelledError, ControllerNotConnectedError):
        # Inside a case, wait_for_controller only reports "not connected" once
        # the run is no longer STARTED, i.e. it was aborted.
        logger.info("Test run cancelled for pivot %d", pivot_id)
        pivot.refresh_from_db()
        if pivot.status != TestRunTestCaseStatus.CANCELLED:
            _mark_pivot_cancelled(pivot)
    except ControllerReconnectTimeoutError as exc:
        logger.warning("Aborting test run for pivot %d: %s", pivot_id, exc)
        _abort_run_after_reconnect_timeout(pivot, exc)
    except Exception as exc:
        # Infrastructure faults, the soft time limit and bugs are never a
        # verdict on the app: block the case and let the chain continue.
        _settle_pivot_after_error(pivot, exc)
    except BaseException as exc:
        logger.critical(
            "execute_test_run_test_case hit BaseException for pivot %d: %s",
            pivot_id,
            exc,
        )
        _settle_pivot_after_error(pivot, exc)
        raise
    finally:
        _safe_cleanup(project.id, wait_for_runner=False)
        _update_test_run_status_if_needed(pivot.test_run)


def _abort_run_after_reconnect_timeout(
    pivot: TestRunTestCase, exc: ControllerReconnectTimeoutError
) -> None:
    """Block this case, then cancel the rest of the run without revoking this task."""
    pivot.refresh_from_db(fields=["status"])
    if pivot.status == TestRunTestCaseStatus.IN_PROGRESS:
        reason = BlockedReason.RUNNER_DISCONNECTED
        _mark_pivot_blocked(pivot, reason, blocked_message(exc, reason))

    test_run = pivot.test_run
    test_run.refresh_from_db(fields=["status"])
    # This task may be the chain's last one (celery_task_id); revoking it would
    # SIGTERM itself mid-cleanup. Later tasks early-exit on CANCELLED. Only
    # pending and in-progress cases are touched, so this one stays Blocked.
    abort_test_run(test_run, reason=str(exc), revoke_task=False)


def _settle_pivot_after_error(pivot: TestRunTestCase, exc: BaseException) -> None:
    """Record an execution error on a case that has no verdict yet.

    A case the run's abort already settled is left alone; a run cancelled in
    the meantime cancels the case; anything else blocks it.
    """
    pivot.refresh_from_db(fields=["status"])
    if pivot.status != TestRunTestCaseStatus.IN_PROGRESS:
        logger.warning(
            "Execution error for pivot %d after it reached %s: %r",
            pivot.id,
            pivot.status,
            exc,
        )
        return

    test_run = pivot.test_run
    test_run.refresh_from_db(fields=["status"])
    if test_run.status == TestRunStatus.CANCELLED:
        logger.info("Test run cancelled for pivot %d (%r)", pivot.id, exc)
        _mark_pivot_cancelled(pivot)
        return

    reason = classify_case_exception(exc)
    if reason == BlockedReason.INTERNAL_ERROR:
        logger.exception("Blocking pivot %d after an unexpected error", pivot.id)
    else:
        logger.warning("Blocking pivot %d (%s): %s", pivot.id, reason.value, exc)
    _mark_pivot_blocked(pivot, reason, blocked_message(exc, reason))


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
        _broadcast_test_run_status(test_run)


def _build_log_callback(pivot: TestRunTestCase) -> Callable[[str], None]:
    def _append_log(message: str) -> None:
        pivot.logs += message + "\n"
        pivot.save(update_fields=["logs", "updated_at"])
        _broadcast_log(pivot, message)

    return _append_log


def _build_screenshot_callback(pivot: TestRunTestCase) -> ScreenshotCallback:
    def _save_screenshot(base64_data: str, tool_name: str) -> None:
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


def _extract_agent_summary(result: AgentResult) -> str:
    for message in reversed(result.messages):
        if message.role == "assistant" and isinstance(message.content, str):
            return message.content
    return result.error or ""


def _mark_pivot_blocked(
    pivot: TestRunTestCase, reason: BlockedReason, message: str
) -> None:
    """Mark a case Blocked: it could not be executed or judged, no verdict."""
    log_line = f"[Blocked: {reason.label}] {message}"
    pivot.logs += log_line + "\n"
    pivot.status = TestRunTestCaseStatus.BLOCKED
    pivot.blocked_reason = reason
    pivot.result = message
    pivot.finished_at = timezone.now()
    pivot.save(
        update_fields=[
            "status",
            "blocked_reason",
            "result",
            "finished_at",
            "logs",
            "updated_at",
        ]
    )
    _broadcast_log(pivot, log_line)
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)


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
    if test_run.status != TestRunStatus.STARTED:
        return

    test_run.status = TestRunStatus.CANCELLED
    test_run.finished_at = timezone.now()
    test_run.save(update_fields=["status", "finished_at", "updated_at"])

    for pivot in test_run.pivot_entries.filter(
        status=TestRunTestCaseStatus.IN_PROGRESS
    ):
        _mark_pivot_cancelled(pivot)

    for pivot in test_run.pivot_entries.filter(status=TestRunTestCaseStatus.CREATED):
        _mark_pivot_cancelled(pivot)

    _broadcast_test_run_status(test_run)

    celery_task_id = test_run.celery_task_id
    if revoke_task and celery_task_id:
        transaction.on_commit(
            lambda: celery_app.control.revoke(celery_task_id, terminate=True)
        )


def abort_active_test_run_on_disconnect(project: Project) -> None:
    active_run = TestRun.objects.filter(
        project=project, status=TestRunStatus.STARTED
    ).first()
    if active_run is not None:
        abort_test_run(active_run, reason="Controller client disconnected")


def _update_test_run_status_if_needed(test_run: TestRun) -> None:
    test_run.refresh_from_db()
    if test_run.status == TestRunStatus.CANCELLED:
        return

    all_pivots = test_run.pivot_entries.all()

    if not all_pivots.exists():
        return

    all_done = not all_pivots.filter(
        status__in=[TestRunTestCaseStatus.CREATED, TestRunTestCaseStatus.IN_PROGRESS]
    ).exists()

    if all_done:
        test_run.status = TestRunStatus.DONE
        test_run.finished_at = timezone.now()
        test_run.save(update_fields=["status", "finished_at", "updated_at"])
        _broadcast_test_run_status(test_run)


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
