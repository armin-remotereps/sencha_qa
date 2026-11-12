from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
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
    Avg,
    Case,
    Count,
    Exists,
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
from agents.checkpoint import (
    person_mark_verdict,
    read_checkpoint_help,
    read_checkpoint_person_step,
    with_person_step,
)
from agents.types import (
    AgentCancelledError,
    AgentResult,
    AgentStopReason,
    AttemptScreenshotCallback,
    CancellationCheck,
    CheckpointPersonStep,
    HelpRequestInfo,
    LogCallback,
    OrchestratorCheckpoint,
    PersonMarkVerdict,
    PersonStepHandOff,
    PixelParseResult,
)
from auto_tester.celery import app as celery_app
from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    ARTIFACT_PATH_MAX_CHARS,
    CLIENT_VERSION,
    INSPECT_PATHS_MAX,
    REQUIRED_CLIENT_CAPABILITIES,
    SCAN_DIRECTORY_MAX_DEPTH,
    SCAN_DIRECTORY_MAX_ENTRIES,
    BrowserName,
    ClientCapability,
    ErrorCode,
    PointerModifier,
    ScrollDirection,
    WebBrowser,
    WebBrowserSetupStep,
    is_mobile_target,
    parse_inspect_paths_result,
    parse_scan_directory_result,
    parse_web_browser_list_details,
)
from knowledge_base.services.item_processing import COPY_PROJECT_KNOWLEDGE_TASK
from knowledge_base.services.knowledge_index import knowledge_index_text
from machine_readiness.profile_text import artifacts_profile_text
from machine_readiness.run_stage0_state import Stage0Summary, run_stage0_summary
from machine_readiness.types import InventoryEntry
from machine_setup.auto_fix_stop import request_auto_fix_stop
from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE, live_turn_exists
from notifications.services.inbox_broadcast import broadcast_project_attention_changed
from projects.application_context_services import write_application_context
from projects.call_deadline import capped_timeout
from projects.case_checkpoint_services import (
    build_checkpoint_callback,
    load_resume_checkpoint,
)
from projects.case_credit import credit_cases
from projects.case_duration_services import (
    AVERAGE_CASE_STATUSES,
    CASE_SEGMENT_OPEN_FIELDS,
    close_case_segment,
    open_case_segment,
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
    enqueue_open_paused_help_chat,
    get_run_help_state,
    mark_auto_fix_running,
)
from projects.case_pause_policy import (
    pause_ask,
    pause_continue_mode,
    pause_log_line,
)
from projects.case_pause_rows import (
    PAUSED_CASE_FIELDS,
    PauseRequestSnapshot,
    clear_pause_request,
    mark_case_paused_for_help,
    open_pause_help_request,
    pause_pending_for,
    read_pause_request,
)
from projects.case_review_redesign_store import supersede_run_review_drafts
from projects.case_step_progress import (
    StepProgressPayload,
    case_step_payload,
    pivot_step_payload,
)
from projects.case_step_refs import case_step_rows, case_steps_label
from projects.case_task_text import (
    build_task_description,
    numbered_case_step_count,
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
    DeviceDisconnectedError,
    KeptSessionLimitError,
    TestRunnerDisconnectedError,
    TestRunnerUnresponsiveError,
    WebBrowserAmbiguousError,
    WebBrowserNotOpenError,
    WebBrowserScriptError,
    WebBrowserSetupNeededError,
    WebBrowserUnsupportedError,
)
from projects.manual_run_policy import is_manual_mode
from projects.manual_submission_supersede import supersede_manual_submissions
from projects.models import (
    ACTIVE_RUN_STATUSES,
    HELP_REQUEST_TEXT_MAX_LENGTH,
    MANUAL_CLAIM_CLEARED,
    MANUAL_CLAIM_FIELDS,
    OPEN_PERSON_STEP_STATUSES,
    PAUSE_REQUEST_CLEARED,
    PAUSE_REQUEST_FIELDS,
    RETEST_CLEARED,
    WRITABLE_ROLES,
    ApplicationPlatform,
    BlockedReason,
    CaseHelpRequest,
    CasePersonStep,
    CasePlanRevision,
    CaseResultChange,
    CaseResultEdit,
    ContextWriteSource,
    FailedBy,
    HelpAutoFixState,
    HelpContinueMode,
    HelpProposalAction,
    HelpResolution,
    PersonStepClose,
    PersonStepStatus,
    PlanRevisionStatus,
    Project,
    ProjectMembership,
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
from projects.person_mark_verdict import (
    apply_person_mark,
    settle_person_marks_on_abort,
)
from projects.person_step_notifications import notify_person_step
from projects.person_step_rows import (
    build_person_step_settler,
    cap_text,
    command_shell_label,
    hand_back_person_step_row,
    open_person_step_row,
    settle_person_steps,
    with_person_request_id,
)
from projects.project_access_services import get_project_access
from projects.run_case_order import PICKER_CASE_ORDERING, RUN_CASE_ORDERING
from projects.run_finished_notifications import notify_run_finished
from projects.run_help_mode_policy import resolve_help_mode
from projects.run_runner_assignment import (
    lock_runner_to_assign,
    lock_usable_runner,
    write_runner_assignment,
)
from projects.run_runner_gate import (
    CHOOSE_RUNNER_MESSAGE,
    RUNNER_CHANGED_MESSAGE,
    RUNNERS_PAGE_URL_NAME,
)
from projects.runner_person_step_push import nudge_runner_person_steps
from projects.runner_use import runner_hold, runner_id_of_run
from runners.channels import runner_group
from runners.exceptions import RunnerError
from runners.machine_state import (
    DEVICE_NOT_READY_MESSAGE,
    MachineState,
    device_offline,
    runner_machine_state,
)
from runners.models import TestRunner
from runners.services.authenticate_runner import outdated_runner_message
from runners.types import RunnerId
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import (
    freeze_session_cases_of_run,
    record_case_activity,
)
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
        runner_id: RunnerId,
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
    *,
    source_project: Project,
    user: CustomUser,
    name: str,
    copy_knowledge_base: bool = True,
) -> Project:
    """Copies tags and test cases (without upload association) from the source.

    ``copy_knowledge_base``: the source's ready knowledge base items are copied
    by a task queued after commit (the new project must exist for it).
    """
    new_project = Project.objects.create(
        name=name, default_help_mode=source_project.default_help_mode
    )
    _sync_tags(new_project, list(source_project.tags.values_list("name", flat=True)))
    new_project.members.add(user)
    source_cases = TestCase.objects.filter(project=source_project)
    copies = [
        _clone_test_case(source=tc, target_project=new_project) for tc in source_cases
    ]
    TestCase.objects.bulk_create(copies)
    if copy_knowledge_base:
        source_id, new_id = source_project.id, new_project.id
        transaction.on_commit(
            lambda: celery_app.send_task(
                COPY_PROJECT_KNOWLEDGE_TASK, args=[source_id, new_id]
            )
        )
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
    """Projects the user may copy cases into (writable member), by name."""
    return (
        Project.objects.filter(
            memberships__user=user,
            memberships__role__in=WRITABLE_ROLES,
            archived=False,
        )
        .exclude(id=exclude_project.id)
        .order_by("name")
    )


def get_project_for_user(project_id: int, user: CustomUser) -> Project | None:
    """The project for its WebSockets: same rule as the page decorators."""
    access = get_project_access(project_id, user)
    return access.project if access is not None else None


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
        viewer_can_write=Exists(
            ProjectMembership.objects.filter(
                project=OuterRef("pk"), user=user, role__in=WRITABLE_ROLES
            )
        ),
    ).order_by("-created_at")

    if search:
        qs = qs.filter(name__icontains=search)

    if tag_filter:
        qs = qs.filter(tags__name=tag_filter)

    paginator: Paginator[Project] = Paginator(qs, per_page)
    return paginator.get_page(page)


def get_latest_runs_by_project(projects: Iterable[Project]) -> dict[int, TestRun]:
    """Fetch each project's latest run with its outcome counts, in one query.

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
        retest_count=Count(
            "pivot_entries",
            filter=Q(pivot_entries__status=TestRunTestCaseStatus.NEEDS_RETEST),
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
    machine_state: MachineState | None,
    active_run: TestRun | None,
    latest_run: TestRun | None,
) -> PrimaryAction:
    """Pick a project's primary action; shared by the overview and dashboard cards.

    ``machine_state``: the state of the active run's Test Runner, or None when
    no run holds one (runners belong to users, so a project without an active
    run has no machine to connect).
    """
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
    # The active run's Test Runner state; None when no active run holds one.
    machine_state: MachineState | None
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
    """Annotate a `TestRun` queryset with its case and outcome counts.

    Shared by `list_test_runs_for_project` and `get_project_overview` so
    both surfaces report the same numbers from the same query shape.
    ``avg_case_duration`` is the mean active duration of the Passed / Failed
    cases with a known duration (None when there is none), on the same join.
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
        retest_count=Count(
            "pivot_entries",
            filter=Q(pivot_entries__status=TestRunTestCaseStatus.NEEDS_RETEST),
        ),
        avg_case_duration=Avg(
            "pivot_entries__active_duration",
            filter=Q(pivot_entries__status__in=AVERAGE_CASE_STATUSES),
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
    # Phase 1 keeps one active run per project; its runner is "the machine".
    active_run = (
        TestRun.objects.filter(project=project, status__in=ACTIVE_RUN_STATUSES)
        .select_related("runner")
        .first()
    )
    machine_state = (
        runner_machine_state(active_run.runner, visual_engine)
        if active_run is not None and active_run.runner is not None
        else None
    )
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
    runner: TestRunner | None, machine_state: MachineState
) -> ReadinessCheck:
    """The run's chosen Test Runner is connected (and its phone ready)."""
    # A phone runner's device not READY reads DEVICE_OFFLINE, or STARTING
    # while its session is created (the engine starting passes this check).
    device_not_ready = (
        runner is not None
        and machine_state in (MachineState.DEVICE_OFFLINE, MachineState.STARTING)
        and device_offline(runner)
    )
    passed = machine_state != MachineState.DISCONNECTED and not device_not_ready
    if passed:
        title = "Test Runner connected"
        help_text = ""
    elif runner is None:
        title = "No Test Runner chosen"
        help_text = CHOOSE_RUNNER_MESSAGE
    elif device_not_ready:
        title = "Phone or tablet not connected"
        help_text = DEVICE_NOT_READY_MESSAGE
    else:
        title = "Test Runner not connected"
        help_text = "Runs need a connected Test Runner to execute steps."
    return ReadinessCheck(
        key="machine_connected",
        passed=passed,
        blocking=True,
        title=title,
        help=help_text,
        link_url=reverse(RUNNERS_PAGE_URL_NAME),
    )


def _runner_free_check(
    runner: TestRunner | None, test_run: TestRun | None
) -> ReadinessCheck:
    """The chosen Test Runner serves no other holder (``runner_use``).

    Passes without a runner (``_machine_connected_check`` reports that). The
    run itself, already holding its runner, does not count.
    """
    hold = (
        runner_hold(
            runner.runner_id,
            ignore_run_id=test_run.pk if test_run is not None else None,
        )
        if runner is not None
        else None
    )
    if hold is None:
        title = "Test Runner is free"
        help_text = ""
    else:
        title = "Test Runner is busy"
        help_text = hold.message
    return ReadinessCheck(
        key="runner_free",
        passed=hold is None,
        blocking=True,
        title=title,
        help=help_text,
        link_url=reverse(RUNNERS_PAGE_URL_NAME),
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


# The run page's "Machine for this run" card (machine_readiness run stage 0).
RUN_STAGE0_ANCHOR: Final = "run-stage0"


def _machine_prepared_check(
    project: Project,
    test_run: TestRun,
    summary: Stage0Summary | None,
    runner: TestRunner | None,
) -> ReadinessCheck:
    """Warning only (plan Q6): the run's stage-0 requirements.

    2 queries, none when the caller passes the ``summary`` it already has.
    """
    if summary is None:
        summary = run_stage0_summary(
            project,
            test_run.id,
            runner_system_info=runner.system_info if runner is not None else None,
        )
    passed = summary.prepared
    gaps: list[str] = []
    if summary.missing:
        gaps.append(f"{summary.missing} missing")
    if summary.mismatch:
        gaps.append(
            f"{summary.mismatch} version mismatch"
            f"{'es' if summary.mismatch != 1 else ''}"
        )
    if summary.unknown:
        gaps.append(f"{summary.unknown} not checked")
    if summary.unanalysed_case_count:
        count = summary.unanalysed_case_count
        gaps.append(f"{count} case{'s' if count != 1 else ''} not analysed")
    if passed:
        title = (
            "Machine has what this run needs"
            if summary.requirement_count
            else "This run's cases need nothing installed"
        )
        help_text = ""
    else:
        title = "Machine may not be prepared for this run"
        help_text = (
            f"{', '.join(gaps).capitalize()}. Use Check machine or Prepare "
            "machine on this run's card; this does not block the run."
        )
    return ReadinessCheck(
        key="machine_prepared",
        passed=passed,
        blocking=False,
        title=title,
        help=help_text,
        link_url=(
            reverse("projects:test_run_detail", args=[project.id, test_run.id])
            + f"#{RUN_STAGE0_ANCHOR}"
        ),
    )


def _load_readiness_runner(
    test_run: TestRun | None,
    runner_id: RunnerId | None,
    loaded_runners: Sequence[TestRunner] = (),
) -> TestRunner | None:
    """The runner the checklist is about: ``runner_id`` (the Start form's
    choice), else the run's assigned one; None when neither (1 query at most,
    none when it is among ``loaded_runners``)."""
    effective = runner_id
    if effective is None and test_run is not None:
        effective = runner_id_of_run(test_run)
    if effective is None:
        return None
    for loaded in loaded_runners:
        if loaded.pk == effective and loaded.archived_at is None:
            return loaded
    return TestRunner.objects.filter(pk=effective, archived_at__isnull=True).first()


def get_run_readiness(
    project: Project,
    *,
    test_run: TestRun | None = None,
    selected_count: int | None = None,
    stage0_summary: Stage0Summary | None = None,
    runner_id: RunnerId | None = None,
    visual_engine: VisualEngineStatus | None = None,
    loaded_runners: Sequence[TestRunner] = (),
) -> RunReadiness:
    """Compute the "Ready to run?" checklist shown before starting a run.

    ``runner_id``: the Test Runner chosen for the run (the Start form's
    default, ``run_runner_default.default_runner_for_start``); None uses the
    run's assigned runner, if any. Agent modes need one that is connected and
    free; the project rules (one active run, one setup turn) stay in Phase 1.
    ``stage0_summary``: the run's stage-0 counts when the caller already
    loaded them (the run page's checklist), so they are not queried twice.
    ``visual_engine`` / ``loaded_runners``: the engine status and the
    viewer's usable runner rows the caller already loaded (the run page);
    the chosen runner is taken from them when it is among them.
    """
    help_mode = (
        test_run.help_mode if test_run is not None else project.default_help_mode
    )
    if is_manual_mode(help_mode):
        # A person performs every step (D11): no visual engine, no machine
        # preparation, and the Test Runner is optional (Q12; it is held only
        # while a case is claimed).
        return RunReadiness(
            checks=(
                _cases_selected_check(test_run, selected_count),
                _no_active_run_check(project, test_run),
                _no_machine_setup_check(project),
                _application_context_check(project),
            )
        )
    if visual_engine is None:
        visual_engine = get_visual_engine_status()
    runner = _load_readiness_runner(test_run, runner_id, loaded_runners)
    machine_state = runner_machine_state(runner, visual_engine)
    checks: tuple[ReadinessCheck, ...] = (
        _cases_selected_check(test_run, selected_count),
        _machine_connected_check(runner, machine_state),
        _runner_free_check(runner, test_run),
        _visual_engine_ready_check(visual_engine),
        _no_active_run_check(project, test_run),
        _no_machine_setup_check(project),
        _application_context_check(project),
    )
    if test_run is not None:
        checks += (_machine_prepared_check(project, test_run, stage0_summary, runner),)
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
        f"server expects {CLIENT_VERSION}. {outdated_runner_message()}"
    )


def _find_missing_capabilities(capabilities: Sequence[str]) -> list[str]:
    present = set(capabilities)
    return sorted(cap for cap in REQUIRED_CLIENT_CAPABILITIES if cap not in present)


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


def _controller_group(runner_id: RunnerId) -> str:
    """The runner's controller socket group (``runners.channels.runner_group``)."""
    return runner_group(runner_id)


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
# Fixed server text: shown in the UI, to the agents and in TestRail.
_DEVICE_DISCONNECTED_MESSAGE = "The phone or tablet is disconnected or still starting."
_TARGET_MISMATCH_MESSAGE = (
    "The Test Runner refused this action: it does not apply to what the "
    "runner drives (a desktop action on a phone or tablet, or the reverse)."
)

# WEB_BROWSER_V1 refusals: fixed server text; the typed fields of the raised
# exceptions carry what the caller needs (never the runner's own text).
_WEB_BROWSER_SETUP_NEEDED_MESSAGE = "The browser needs a one-time setup first."
_WEB_BROWSER_UNSUPPORTED_MESSAGE = "This browser is not supported on the test machine."
_WEB_BROWSER_NOT_OPEN_MESSAGE = "No controllable browser is open."
_WEB_BROWSER_AMBIGUOUS_MESSAGE = "Several browsers are open; name one."
_WEB_SCRIPT_FAILED_MESSAGE = "The script threw an error."
_WEB_SCRIPT_ERROR_CLASS_MAX_CHARS = 64
_WEB_SCRIPT_ERROR_CLASS_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$."
)


def _web_setup_step(details: object) -> WebBrowserSetupStep | None:
    try:
        return WebBrowserSetupStep(str(details).strip())
    except ValueError:
        return None


def _web_browser_named(details: object) -> WebBrowser | None:
    try:
        return WebBrowser(str(details).strip())
    except ValueError:
        return None


def _web_script_error_class(details: object) -> str:
    """The runner's JavaScript error class if it is a plain identifier path
    ([A-Za-z0-9_$.], at most 64 characters), else ``"Error"``."""
    text = str(details).strip()
    if (
        not text
        or len(text) > _WEB_SCRIPT_ERROR_CLASS_MAX_CHARS
        or not set(text) <= _WEB_SCRIPT_ERROR_CLASS_CHARS
    ):
        return "Error"
    return text


def _raise_for_web_browser_error(code: object, details: object, message: str) -> None:
    """Raise the WEB_BROWSER_V1 exception for ``code``; return for any other
    code. The runner's text stays in the server log."""
    if code == ErrorCode.WEB_BROWSER_SETUP_NEEDED:
        logger.info("Controller reports a browser setup step: %s", message)
        raise WebBrowserSetupNeededError(
            _WEB_BROWSER_SETUP_NEEDED_MESSAGE, setup_step=_web_setup_step(details)
        )
    if code == ErrorCode.WEB_BROWSER_UNSUPPORTED:
        raise WebBrowserUnsupportedError(_WEB_BROWSER_UNSUPPORTED_MESSAGE)
    if code == ErrorCode.WEB_BROWSER_NOT_OPEN:
        raise WebBrowserNotOpenError(
            _WEB_BROWSER_NOT_OPEN_MESSAGE, named=_web_browser_named(details)
        )
    if code == ErrorCode.WEB_BROWSER_AMBIGUOUS:
        raise WebBrowserAmbiguousError(
            _WEB_BROWSER_AMBIGUOUS_MESSAGE,
            open_browsers=parse_web_browser_list_details(str(details)),
        )
    if code == ErrorCode.SCRIPT_FAILED:
        raise WebBrowserScriptError(
            _WEB_SCRIPT_FAILED_MESSAGE, error_class=_web_script_error_class(details)
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
    if code == ErrorCode.DEVICE_DISCONNECTED:
        # The runner's text can name the device; it stays in the server log.
        logger.warning("Controller reports the device disconnected: %s", message)
        raise DeviceDisconnectedError(_DEVICE_DISCONNECTED_MESSAGE)
    if code == ErrorCode.TARGET_MISMATCH:
        logger.warning("Controller refused %s for its target: %s", event_type, message)
        raise ControllerActionError(_TARGET_MISMATCH_MESSAGE)
    if code == ErrorCode.TOO_MANY_SESSIONS:
        raise KeptSessionLimitError(message)
    _raise_for_web_browser_error(code, details, message)
    raise ControllerActionError(message)


_T = TypeVar("_T")


class _ConnectionLost(Exception):
    """The connection that received an action dropped before it replied."""


def _run_with_reconnect_retry(
    runner_id: RunnerId,
    event_type: str,
    attempt: Callable[[str], _T],
    *,
    on_resend: Callable[[], None] | None = None,
    resend: bool = True,
    wait_first: bool | None = None,
) -> _T:
    """Run ``attempt`` and re-send it once if the Test Runner connection drops.

    ``attempt`` receives the owning consumer channel and raises
    ``_ConnectionLost`` when the loss rule matches. Each attempt dispatches a
    new request on a new reply channel, so a late reply to the abandoned
    request is never read. Any other failure propagates unchanged. With
    ``resend=False`` a lost connection fails the action, and by default
    nothing waits for the runner either: a missing runner raises
    ``ControllerNotConnectedError``. ``wait_first=True`` still waits for a
    reconnecting runner before the first send (nothing was sent yet).
    """
    owner = (
        wait_for_controller(runner_id)
        if (resend if wait_first is None else wait_first)
        else _current_owner_or_raise(runner_id)
    )
    try:
        return attempt(owner)
    except _ConnectionLost as exc:
        if not resend:
            raise ControllerActionError(
                f"Test Runner connection was lost while running {event_type}; "
                "it may already have been done, so check before repeating it"
            ) from exc
        logger.warning(
            "Test Runner connection lost during %s for runner %d; "
            "waiting to re-send",
            event_type,
            runner_id,
        )

    owner = wait_for_controller(runner_id, lost_owner=owner)
    logger.info("Re-sending %s for runner %d (attempt 2)", event_type, runner_id)
    if on_resend is not None:
        on_resend()
    try:
        return attempt(owner)
    except _ConnectionLost as exc:
        raise TestRunnerDisconnectedError(
            f"Test Runner connection was lost again while running {event_type}; "
            "giving up on this action"
        ) from exc


def _current_owner_or_raise(runner_id: RunnerId) -> str:
    owner = connected_owner(runner_id)
    if owner is None:
        raise ControllerNotConnectedError(
            f"Test Runner is not connected for runner {runner_id}"
        )
    return owner


def _is_stray_disconnect_reply(
    reply: dict[str, Any], runner_id: RunnerId, owner: str
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
    if controller_connection_lost(runner_id, owner):
        raise _ConnectionLost(reply.get("message", ""))
    return True


def _raise_reply_timeout(
    runner_id: RunnerId,
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
    if controller_connection_lost(runner_id, owner):
        raise _ConnectionLost(message) from cause
    if runner_local:
        raise TestRunnerUnresponsiveError(message) from cause
    raise ControllerActionError(message) from cause


def _dispatch_controller_action(
    runner_id: RunnerId,
    event_type: str,
    reply_timeout: float,
    *,
    resend_on_reconnect: bool = True,
    wait_before_send: bool | None = None,
    runner_local: bool = False,
    **payload: Any,
) -> dict[str, Any]:
    layer = _get_channel_layer_or_raise()

    def _attempt(owner: str) -> dict[str, Any]:
        return _send_controller_action_once(
            layer,
            runner_id,
            owner,
            event_type,
            reply_timeout,
            payload,
            runner_local=runner_local,
        )

    return _run_with_reconnect_retry(
        runner_id,
        event_type,
        _attempt,
        resend=resend_on_reconnect,
        wait_first=wait_before_send,
    )


def dispatch_controller_action(
    runner_id: RunnerId,
    event_type: str,
    reply_timeout: float,
    *,
    resend_on_reconnect: bool = True,
    runner_local: bool = False,
    **payload: Any,
) -> dict[str, Any]:
    """Public alias of ``_dispatch_controller_action`` for controller action
    modules outside this legacy module (``controller_mobile_actions``)."""
    return _dispatch_controller_action(
        runner_id,
        event_type,
        reply_timeout,
        resend_on_reconnect=resend_on_reconnect,
        runner_local=runner_local,
        **payload,
    )


def _send_controller_action_once(
    layer: Any,
    runner_id: RunnerId,
    owner: str,
    event_type: str,
    reply_timeout: float,
    payload: dict[str, Any],
    *,
    runner_local: bool,
) -> dict[str, Any]:
    # A bounded caller's deadline (``call_deadline``) cuts the wait; raises
    # before anything is sent when no time is left. Unset: unchanged.
    reply_timeout = capped_timeout(reply_timeout)
    reply_channel: str = async_to_sync(layer.new_channel)()
    request_id = str(uuid.uuid4())

    event: dict[str, Any] = {
        "type": event_type,
        "request_id": request_id,
        "reply_channel": reply_channel,
        **payload,
    }
    async_to_sync(layer.group_send)(_controller_group(runner_id), event)

    deadline = time.monotonic() + reply_timeout
    timeout_message = (
        f"Timed out waiting for controller reply to {event_type} "
        f"after {reply_timeout}s"
    )
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _raise_reply_timeout(
                runner_id, owner, timeout_message, None, runner_local=runner_local
            )

        try:
            result: dict[str, Any] = async_to_sync(asyncio.wait_for)(
                layer.receive(reply_channel), timeout=remaining
            )
        except asyncio.TimeoutError as exc:
            _raise_reply_timeout(
                runner_id, owner, timeout_message, exc, runner_local=runner_local
            )

        if _is_stray_disconnect_reply(result, runner_id, owner):
            continue
        _raise_for_error_reply(result, event_type)
        return result


def _build_action_result(reply: dict[str, Any]) -> ActionResult:
    return ActionResult(
        success=reply.get("success", False),
        message=reply.get("message", ""),
        duration_ms=reply.get("duration_ms", 0.0),
    )


def build_action_result(reply: dict[str, Any]) -> ActionResult:
    """Public alias of ``_build_action_result`` (``controller_mobile_actions``)."""
    return _build_action_result(reply)


def _modifiers_field(modifiers: Sequence[PointerModifier]) -> dict[str, list[str]]:
    """The ``modifiers`` key, only when some are held.

    Without modifiers the message stays byte-identical to older servers', and
    a runner without POINTER_MODIFIERS_V1 is never sent the field (callers
    check the capability first).
    """
    return {"modifiers": [m.value for m in modifiers]} if modifiers else {}


def controller_click(
    runner_id: RunnerId,
    x: int,
    y: int,
    button: str = "left",
    timeout: float = 30.0,
    *,
    modifiers: Sequence[PointerModifier] = (),
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.click",
        timeout,
        x=x,
        y=y,
        button=button,
        runner_local=True,
        # A modified press is not resent after a reconnect: done twice it
        # undoes itself (Ctrl+click). It still waits for a reconnect before
        # the first send, when nothing was done yet.
        resend_on_reconnect=not modifiers,
        wait_before_send=True,
        **_modifiers_field(modifiers),
    )
    return _build_action_result(reply)


def controller_double_click(
    runner_id: RunnerId,
    x: int,
    y: int,
    timeout: float = 30.0,
    *,
    modifiers: Sequence[PointerModifier] = (),
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.double_click",
        timeout,
        x=x,
        y=y,
        runner_local=True,
        # A modified press is not resent after a reconnect: done twice it
        # undoes itself (Ctrl+click). It still waits for a reconnect before
        # the first send, when nothing was done yet.
        resend_on_reconnect=not modifiers,
        wait_before_send=True,
        **_modifiers_field(modifiers),
    )
    return _build_action_result(reply)


def controller_scroll(
    runner_id: RunnerId,
    direction: ScrollDirection,
    amount: int,
    x: int | None,
    y: int | None,
    timeout: float = 30.0,
    *,
    modifiers: Sequence[PointerModifier] = (),
) -> ActionResult:
    """Scroll ``amount`` wheel notches, at screenshot pixel (x, y) or the pointer.

    ``x``/``y`` are sent only when given: the message builder skips keys the
    event lacks, so the runner then scrolls at the current pointer position.
    """
    # Any: a dict[str, int] splat would also have to fit the bool keyword
    # runner_local of _dispatch_controller_action, which mypy rejects.
    point: dict[str, Any] = {} if x is None or y is None else {"x": x, "y": y}
    reply = _dispatch_controller_action(
        runner_id,
        "controller.scroll",
        timeout,
        runner_local=True,
        # A modified press is not resent after a reconnect: done twice it
        # undoes itself (Ctrl+click). It still waits for a reconnect before
        # the first send, when nothing was done yet.
        resend_on_reconnect=not modifiers,
        wait_before_send=True,
        direction=direction.value,
        amount=amount,
        **point,
        **_modifiers_field(modifiers),
    )
    return _build_action_result(reply)


def controller_hover(
    runner_id: RunnerId,
    x: int,
    y: int,
    timeout: float = 30.0,
    *,
    modifiers: Sequence[PointerModifier] = (),
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.hover",
        timeout,
        x=x,
        y=y,
        runner_local=True,
        # A modified press is not resent after a reconnect: done twice it
        # undoes itself (Ctrl+click). It still waits for a reconnect before
        # the first send, when nothing was done yet.
        resend_on_reconnect=not modifiers,
        wait_before_send=True,
        **_modifiers_field(modifiers),
    )
    return _build_action_result(reply)


def controller_drag(
    runner_id: RunnerId,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str = "left",
    duration: float = 0.5,
    timeout: float = 30.0,
    *,
    modifiers: Sequence[PointerModifier] = (),
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.drag",
        timeout,
        runner_local=True,
        # A modified press is not resent after a reconnect: done twice it
        # undoes itself (Ctrl+click). It still waits for a reconnect before
        # the first send, when nothing was done yet.
        resend_on_reconnect=not modifiers,
        wait_before_send=True,
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
        button=button,
        duration=duration,
        **_modifiers_field(modifiers),
    )
    return _build_action_result(reply)


def controller_type_text(
    runner_id: RunnerId,
    text: str,
    interval: float = 0.0,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.type_text",
        timeout,
        text=text,
        interval=interval,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_key_press(
    runner_id: RunnerId,
    keys: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
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
    runner_id: RunnerId,
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
                runner_id, "controller.screenshot", timeout, runner_local=True
            )
        except TestRunnerUnresponsiveError:
            logger.warning(
                "Screenshot timed out for runner %d (attempt %d/%d); retrying",
                runner_id,
                attempt,
                _SCREENSHOT_ATTEMPTS,
            )
            continue
        return _build_screenshot_result(reply, "take a screenshot")

    try:
        reply = _dispatch_controller_action(
            runner_id, "controller.screenshot", timeout, runner_local=True
        )
    except TestRunnerUnresponsiveError as exc:
        raise TestRunnerUnresponsiveError(
            f"Test Runner did not return a screenshot after "
            f"{_SCREENSHOT_ATTEMPTS} attempts of {timeout:.0f}s each"
        ) from exc
    return _build_screenshot_result(reply, "take a screenshot")


def controller_find_elements(
    runner_id: RunnerId,
    box_threshold: float | None = None,
    iou_threshold: float | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> PixelParseResult:
    """Screenshot the test machine, then have a vision worker parse it.

    The elements are in the screenshot's pixel space, which is the space the
    runner's clicks use, so callers use their centers unchanged.
    """
    shot = controller_screenshot(runner_id)
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
    runner_id: RunnerId,
    command: str,
) -> CommandResult:
    return controller_run_command_streaming(runner_id, command)


def controller_run_command_streaming(
    runner_id: RunnerId,
    command: str,
    on_output: Callable[[str, str], None] | None = None,
) -> CommandResult:
    layer = _get_channel_layer_or_raise()
    timeout = float(settings.AGENT_TIMEOUT_SECONDS)

    def _attempt(owner: str) -> CommandResult:
        reply_channel: str = async_to_sync(layer.new_channel)()
        request_id = str(uuid.uuid4())
        _dispatch_run_command_event(
            layer, runner_id, request_id, reply_channel, command
        )
        return _receive_streaming_command_result(
            layer, runner_id, owner, reply_channel, timeout, on_output
        )

    def _emit_rerun_marker() -> None:
        if on_output is not None:
            on_output(_RERUN_COMMAND_MARKER, "stderr")

    return _run_with_reconnect_retry(
        runner_id, _RUN_COMMAND_EVENT, _attempt, on_resend=_emit_rerun_marker
    )


def _dispatch_run_command_event(
    layer: Any,
    runner_id: RunnerId,
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
    async_to_sync(layer.group_send)(_controller_group(runner_id), event)


def _receive_streaming_command_result(
    layer: Any,
    runner_id: RunnerId,
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
            _raise_reply_timeout(runner_id, owner, timeout_message, None)

        try:
            reply: dict[str, Any] = async_to_sync(asyncio.wait_for)(
                layer.receive(reply_channel), timeout=remaining
            )
        except asyncio.TimeoutError as exc:
            _raise_reply_timeout(runner_id, owner, timeout_message, exc)

        if _is_stray_disconnect_reply(reply, runner_id, owner):
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
    runner_id: RunnerId,
    command: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.start_interactive_cmd",
        float(settings.INTERACTIVE_CMD_TIMEOUT_SECONDS),
        command=command,
    )
    return _build_interactive_command_result(reply)


def controller_send_input(
    runner_id: RunnerId,
    session_id: str,
    input_text: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.send_input",
        float(settings.INTERACTIVE_CMD_TIMEOUT_SECONDS),
        session_id=session_id,
        input_text=input_text,
    )
    return _build_interactive_command_result(reply)


def controller_wait_for_command(
    runner_id: RunnerId,
    session_id: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.wait_for_command",
        float(settings.INTERACTIVE_CMD_TIMEOUT_SECONDS),
        session_id=session_id,
    )
    return _build_interactive_command_result(reply)


def controller_terminate_interactive_command(
    runner_id: RunnerId,
    session_id: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        runner_id,
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
    runner_id: RunnerId,
    url: str,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_navigate",
        timeout,
        url=url,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_click(
    runner_id: RunnerId,
    element_index: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_click",
        timeout,
        element_index=element_index,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_double_click(
    runner_id: RunnerId,
    element_index: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_double_click",
        timeout,
        element_index=element_index,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_type(
    runner_id: RunnerId,
    element_index: int,
    text: str,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_type",
        timeout,
        element_index=element_index,
        text=text,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_hover(
    runner_id: RunnerId,
    element_index: int,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_hover",
        timeout,
        element_index=element_index,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_get_elements(
    runner_id: RunnerId,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_get_elements",
        timeout,
        browser=browser.value,
    )
    return _build_browser_content_result(reply)


def controller_browser_get_page_content(
    runner_id: RunnerId,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_get_page_content",
        timeout,
        browser=browser.value,
    )
    return _build_browser_content_result(reply)


def controller_browser_get_url(
    runner_id: RunnerId,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_get_url",
        timeout,
        browser=browser.value,
    )
    return _build_browser_content_result(reply)


def controller_browser_download(
    runner_id: RunnerId,
    url: str,
    save_path: str = "",
    *,
    browser: BrowserName,
    timeout: float = 120.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.browser_download",
        timeout,
        url=url,
        save_path=save_path,
        browser=browser.value,
    )
    return _build_action_result(reply)


def controller_browser_list_downloads(
    runner_id: RunnerId,
    timeout: float = 310.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id, "controller.browser_list_downloads", timeout
    )
    return _build_action_result(reply)


def controller_launch_app(
    runner_id: RunnerId,
    app_name: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id, "controller.launch_app", timeout, app_name=app_name
    )
    return _build_action_result(reply)


def controller_close_app(
    runner_id: RunnerId,
    app_name: str,
    *,
    force: bool = False,
    timeout: float = 30.0,
) -> ActionResult:
    # The 30 s timeout is well above the runner's 5 s wait for the app to quit.
    reply = _dispatch_controller_action(
        runner_id,
        "controller.close_app",
        timeout,
        app_name=app_name,
        force=force,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_close_tab(
    runner_id: RunnerId,
    app_name: str,
    *,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.close_tab",
        timeout,
        app_name=app_name,
        runner_local=True,
    )
    return _build_action_result(reply)


def controller_check_app_installed(
    runner_id: RunnerId,
    app_name: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.check_app_installed",
        timeout,
        app_name=app_name,
        runner_local=True,
    )
    return _build_action_result(reply)


@dataclass(frozen=True)
class DirectoryScan:
    """A ``scan_directory`` answer, validated (ARTIFACT_SCAN_V1)."""

    # The expanded, normalized absolute path the runner scanned.
    root: str
    # Relative to ``root``, breadth first.
    entries: tuple[InventoryEntry, ...]
    # Cut short by ``max_entries`` or the runner's 10 s budget.
    truncated: bool
    permission_errors: int
    # Why nothing was scanned (relative, missing, not a directory, a link,
    # unreadable); "" on success. Runner text: may name local paths.
    error: str


@dataclass(frozen=True)
class PathInfo:
    """One ``inspect_paths`` answer (stat only; ``path`` as requested)."""

    path: str
    exists: bool
    is_dir: bool
    size: int
    mtime: float


def _check_artifact_path(path: str) -> None:
    if not path.strip() or len(path) > ARTIFACT_PATH_MAX_CHARS or "\x00" in path:
        raise ValueError("Expected a non-empty path without NUL characters")


def controller_scan_directory(
    runner_id: RunnerId,
    path: str,
    *,
    max_depth: int,
    max_entries: int,
    allow_network: bool = False,
    timeout: float = 30.0,
) -> DirectoryScan:
    """List a directory tree on the test machine (needs ARTIFACT_SCAN_V1).

    The caller checks ``runner_supports``; an older runner answers
    UNKNOWN_COMMAND (ControllerActionError). A refused root comes back as
    ``error``, not as an exception. Raises ValueError for out-of-range limits.
    ``allow_network`` lets the runner open a network (UNC) root: pass it only
    for a base directory a person entered.
    """
    _check_artifact_path(path)
    if not 1 <= max_depth <= SCAN_DIRECTORY_MAX_DEPTH:
        raise ValueError(f"max_depth must be 1..{SCAN_DIRECTORY_MAX_DEPTH}")
    if not 1 <= max_entries <= SCAN_DIRECTORY_MAX_ENTRIES:
        raise ValueError(f"max_entries must be 1..{SCAN_DIRECTORY_MAX_ENTRIES}")
    # The 30 s timeout is well above the runner's 10 s scan budget.
    reply = _dispatch_controller_action(
        runner_id,
        "controller.scan_directory",
        timeout,
        path=path,
        max_depth=max_depth,
        max_entries=max_entries,
        allow_network=allow_network,
        runner_local=True,
    )
    try:
        scan = parse_scan_directory_result(reply.get("result"), max_entries=max_entries)
    except ProtocolError as exc:
        raise ControllerActionError(f"Invalid scan_directory reply: {exc}") from exc
    return DirectoryScan(
        root=scan.root,
        entries=tuple(
            InventoryEntry(p=entry.p, d=entry.d, s=entry.s, m=entry.m)
            for entry in scan.entries
        ),
        truncated=scan.truncated,
        permission_errors=scan.permission_errors,
        error=scan.error,
    )


def controller_inspect_paths(
    runner_id: RunnerId,
    paths: Sequence[str],
    *,
    allow_network: bool = False,
    timeout: float = 30.0,
) -> list[PathInfo]:
    """Stat up to INSPECT_PATHS_MAX absolute paths (needs ARTIFACT_SCAN_V1).

    Missing, relative or unreadable paths come back with ``exists=False``;
    so do network (UNC) paths unless ``allow_network`` (only for paths under
    a base directory a person entered). Raises ValueError for an empty or
    oversized list.
    """
    if not 1 <= len(paths) <= INSPECT_PATHS_MAX:
        raise ValueError(f"paths must hold 1..{INSPECT_PATHS_MAX} items")
    for path in paths:
        _check_artifact_path(path)
    requested = list(paths)
    reply = _dispatch_controller_action(
        runner_id,
        "controller.inspect_paths",
        timeout,
        paths=requested,
        allow_network=allow_network,
        runner_local=True,
    )
    try:
        infos = parse_inspect_paths_result(reply.get("result"), paths=requested)
    except ProtocolError as exc:
        raise ControllerActionError(f"Invalid inspect_paths reply: {exc}") from exc
    return [
        PathInfo(
            path=info.path,
            exists=info.exists,
            is_dir=info.is_dir,
            size=info.size,
            mtime=info.mtime,
        )
        for info in infos
    ]


def controller_cleanup_environment(
    runner_id: RunnerId,
    timeout: float = 30.0,
    *,
    resend_on_reconnect: bool = True,
) -> ActionResult:
    reply = _dispatch_controller_action(
        runner_id,
        "controller.cleanup_environment",
        timeout,
        resend_on_reconnect=resend_on_reconnect,
        runner_local=True,
    )
    return _build_action_result(reply)


def _safe_cleanup(runner_id: RunnerId, *, wait_for_runner: bool) -> None:
    """Empty the test machine's cleanup folder, never failing the test case.

    Before a case (``wait_for_runner=True``) a dropped runner is waited for like
    any action, and a grace expiry, abort or missing runner propagates so the
    run is aborted or cancelled. After a case nothing waits for the runner: a
    missing runner or a drop during the cleanup is only logged.
    """
    try:
        timeout = float(settings.CLEANUP_TIMEOUT_SECONDS)
        result = controller_cleanup_environment(
            runner_id, timeout=timeout, resend_on_reconnect=wait_for_runner
        )
        if result["success"]:
            logger.info(
                "Environment cleanup for runner %d: %s", runner_id, result["message"]
            )
        else:
            logger.warning(
                "Environment cleanup for runner %d left work undone: %s",
                runner_id,
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
            "Environment cleanup skipped for runner %d", runner_id, exc_info=True
        )
    except Exception:
        logger.warning(
            "Environment cleanup failed for runner %d", runner_id, exc_info=True
        )


def _safe_teardown(
    pivot: TestRunTestCase,
    project: Project,
    runner: TestRunner,
    *,
    generation: int,
    cancellation_check: Callable[[], bool],
    run_case_teardown: _CaseTeardown,
) -> None:
    """Close the apps a Failed case left open, never failing the case.

    Runs after the outcome is written and before ``_safe_cleanup``, so apps that
    hold files in the cleanup folder are gone before it is emptied. Skipped
    while the run is in Human in the loop.
    """
    timeout = settings.CASE_TEARDOWN_TIMEOUT_SECONDS
    if timeout <= 0:
        return
    try:
        pivot.refresh_from_db(fields=["status"])
        if not needs_teardown(pivot.status):
            return
        # Human in the loop: the agent never drives the machine (a teardown is
        # an AI-run interactive close); the person leaves it as they see fit.
        if _build_person_mode_check(pivot.test_run_id)():
            logger.info("Teardown skipped for pivot %d: human in the loop", pivot.pk)
            return
        checkpoint = load_resume_checkpoint(pivot)
        if checkpoint is None:
            logger.info("Teardown skipped for pivot %d: no plan", pivot.pk)
            return
        run_case_teardown(
            checkpoint,
            project.id,
            runner.runner_id,
            timeout_seconds=timeout,
            on_log=_build_log_callback(pivot, generation),
            cancellation_check=cancellation_check,
            system_info=runner.system_info or None,
            project_prompt=build_agent_project_context(project),
            machine_ready=lambda: _machine_can_close_apps(runner.runner_id),
        )
    except Exception:
        logger.warning("Teardown failed for pivot %d", pivot.pk, exc_info=True)


def _machine_can_close_apps(runner_id: RunnerId) -> bool:
    """Runner connected with ``close_app_v1`` and a vision worker ready."""
    row = (
        TestRunner.objects.filter(pk=runner_id, archived_at__isnull=True)
        .values_list("connected", "capabilities")
        .first()
    )
    if row is None:
        return False
    connected, capabilities = row
    return (
        connected
        and ClientCapability.CLOSE_APP_V1.value in (capabilities or [])
        and bool(list_ready_workers())
    )


def controller_browser_take_screenshot(
    runner_id: RunnerId,
    *,
    browser: BrowserName,
    timeout: float = 30.0,
) -> ScreenshotResult:
    reply = _dispatch_controller_action(
        runner_id,
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
    test_run = TestRun.objects.create(
        project=project, name=name, help_mode=project.default_help_mode
    )
    valid_cases = TestCase.objects.filter(id__in=test_case_ids, project=project).only(
        "id", "testrail_source_run_id", "testrail_source_test_id"
    )
    pivots = [
        TestRunTestCase(
            test_run=test_run,
            test_case=tc,
            testrail_source_run_id=tc.testrail_source_run_id,
            testrail_source_test_id=tc.testrail_source_test_id,
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
            testrail_source_test_id=tc.testrail_source_test_id,
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
                "test_run", "test_case", "checked_by", "retest_marked_by"
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
    """List a run's cases: failed, then blocked, then needs re-test, then the rest.

    TestRail order within each group.
    """
    qs = (
        test_run.pivot_entries.select_related(
            "test_case", "checked_by", "retest_marked_by"
        )
        .annotate(
            _failure_priority=Case(
                When(status=TestRunTestCaseStatus.FAILED, then=0),
                When(status=TestRunTestCaseStatus.BLOCKED, then=1),
                When(status=TestRunTestCaseStatus.NEEDS_RETEST, then=2),
                default=3,
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
    # Session rows keep the outcome the reset is about to wipe.
    freeze_session_cases_of_run(test_run.pk)
    # Manual run: lock the cases (the order of a screenshot upload, case then
    # hand-in) and discard their hand-ins, so a late upload is dropped instead
    # of landing on a reset case.
    list(
        TestRunTestCase.objects.select_for_update()
        .filter(test_run_id=test_run.pk)
        .values_list("id", flat=True)
    )
    supersede_manual_submissions(test_run_id=test_run.pk)
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
        pivot.segment_started_at = None
        pivot.active_duration = None
        pivot.checked_at = None
        pivot.checked_by = None
        pivot.testrail_source_result_id = None
        pivot.testrail_source_sent_at = None
        pivot.testrail_source_attachment_count = 0
        pivot.testrail_source_skip_reason = ""
        pivot.execution_checkpoint = {}
        for field, value in RETEST_CLEARED.items():
            setattr(pivot, field, value)
        # A pending pause or a dropped one's message ("Too late to pause").
        clear_pause_request(pivot)
        for field, value in MANUAL_CLAIM_CLEARED.items():
            setattr(pivot, field, value)
        pivot.save(
            update_fields=[
                *RETEST_CLEARED,
                *PAUSE_REQUEST_FIELDS,
                *MANUAL_CLAIM_FIELDS,
                "status",
                "blocked_reason",
                "result",
                "logs",
                "started_at",
                "finished_at",
                "segment_started_at",
                "active_duration",
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
    # Result changes describe results the reset discards; a pending review
    # redesign was built on a checkpoint that is gone.
    CaseResultChange.objects.filter(test_run_test_case__test_run=test_run).delete()
    # Admin edits (and their audit) describe the discarded verdicts too.
    CaseResultEdit.objects.filter(test_run_test_case__test_run=test_run).delete()
    # Person steps (Human in the loop) belong to the discarded checkpoints.
    CasePersonStep.objects.filter(test_run_test_case__test_run=test_run).delete()
    supersede_run_review_drafts(test_run.pk)
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
def start_test_run(
    test_run: TestRun,
    *,
    user: CustomUser,
    help_mode: str | None,
    runner_id: RunnerId | None,
) -> None:
    """Start a WAITING run with ``help_mode`` (a ``RunHelpMode`` value) on
    the Test Runner ``runner_id``.

    ``help_mode`` None keeps the run's stored mode. Only a superuser may
    choose a mode other than the stored one (``HelpModeNotAllowedError``
    otherwise). Agent modes need a runner: ``runner_id`` must be one of the
    user's own (``lock_runner_to_assign``, Q2); None keeps a runner already
    assigned to the run, which any writable member may start on (Q3). A
    Manual run's runner is optional (Q12). Every refusal, runner ones
    included, is a ``ValueError`` with a user-safe message.
    """
    if help_mode is not None and help_mode not in RunHelpMode.values:
        raise ValueError("Choose what happens when a case needs help.")
    # The mode decides whether a runner is needed; read before the locks
    # (the runner row is locked first) and re-checked under the run lock.
    stored_help_mode, assigned_runner_id = TestRun.objects.values_list(
        "help_mode", "runner_id"
    ).get(pk=test_run.pk)
    planned_manual = is_manual_mode(
        stored_help_mode if help_mode is None else help_mode
    )
    runner = _lock_runner_for_start(
        test_run,
        user=user,
        runner_id=runner_id,
        assigned_runner_id=(
            None if assigned_runner_id is None else RunnerId(assigned_runner_id)
        ),
        manual=planned_manual,
    )
    # Lock order: runner, then project, then run. The project lock
    # serializes a Start against another Start or Resume in the project, so
    # the "no other run is active" check below cannot race (Phase 1 rule).
    Project.objects.select_for_update().only("id").get(pk=test_run.project_id)
    locked_status, locked_help_mode, locked_runner_id = (
        TestRun.objects.select_for_update()
        .values_list("status", "help_mode", "runner_id")
        .get(pk=test_run.pk)
    )
    resolved_help_mode = resolve_help_mode(
        user=user, requested=help_mode, current=locked_help_mode
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
    now = timezone.now()
    if live_turn_exists(test_run.project_id, now):
        raise ValueError(MACHINE_SETUP_RUNNING_MESSAGE)

    new_help_mode = (
        locked_help_mode if resolved_help_mode is None else resolved_help_mode
    )
    manual = is_manual_mode(new_help_mode)
    if manual != planned_manual or (
        runner_id is None and locked_runner_id != assigned_runner_id
    ):
        raise ValueError(RUNNER_CHANGED_MESSAGE)
    if runner is not None and locked_runner_id != runner.pk:
        write_runner_assignment(test_run, runner, now=now)

    test_run.status = TestRunStatus.STARTED
    test_run.help_mode = new_help_mode
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
    record_case_activity(user=user, pivot_ids=pivot_ids, activity=CaseActivity.START)
    if manual:
        # No execution chain: a person performs the cases at the Test Runner
        # window, which this broadcast nudges to show the run. No credit yet:
        # each hand-in credits its case to the tester (a resolved member).
        transaction.on_commit(lambda: broadcast_test_run_status(test_run))
        return
    credit_cases(user=user, pivot_ids=pivot_ids)

    def _enqueue() -> None:
        if queue_test_run_cases(test_run, pivot_ids):
            broadcast_test_run_status(test_run)

    # Workers must see the STARTED run, so the chain is queued after commit.
    transaction.on_commit(_enqueue)


def _lock_runner_for_start(
    test_run: TestRun,
    *,
    user: CustomUser,
    runner_id: RunnerId | None,
    assigned_runner_id: RunnerId | None,
    manual: bool,
) -> TestRunner | None:
    """Lock the runner a Start uses (before the project lock); None for a
    Manual run started without one. Runner refusals become ``ValueError``."""
    try:
        if runner_id is not None and runner_id != assigned_runner_id:
            return lock_runner_to_assign(
                runner_id, actor=user, project=test_run.project
            )
        if assigned_runner_id is not None:
            # The run's own runner, assigned by its owner (Q3).
            return lock_usable_runner(
                assigned_runner_id,
                project_id=test_run.project_id,
                ignore_run_id=test_run.pk,
            )
    except RunnerError as exc:
        raise ValueError(exc.message) from exc
    if not manual:
        raise ValueError(CHOOSE_RUNNER_MESSAGE)
    return None


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
        needs_retest=Count("id", filter=Q(status=TestRunTestCaseStatus.NEEDS_RETEST)),
        need_help=Count("id", filter=Q(status=TestRunTestCaseStatus.NEED_HELP)),
        awaiting_person=Count(
            "id", filter=Q(status=TestRunTestCaseStatus.AWAITING_PERSON)
        ),
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
            "test_case_id", "testrail_source_run_id", "testrail_source_test_id"
        )
    )
    if not can_rerun_cases(test_run, len(rows)):
        raise ValueError(
            f"Only completed or cancelled runs with {scope.value} cases can be rerun."
        )

    new_run = TestRun.objects.create(
        project=test_run.project,
        # A new run takes the project mode (Start pre-selects it).
        help_mode=test_run.project.default_help_mode,
        name=f"{test_run.display_name} — {scope.value} rerun",
        testrail_project_id=test_run.testrail_project_id,
        testrail_suite_id=test_run.testrail_suite_id,
        testrail_run_id=test_run.testrail_run_id,
    )
    pivots = [
        TestRunTestCase(
            test_run=new_run,
            test_case_id=case_id,
            testrail_source_run_id=source_run_id,
            testrail_source_test_id=source_test_id,
        )
        for case_id, source_run_id, source_test_id in rows
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


def list_retest_case_titles(test_run: TestRun) -> list[str]:
    """First Needs re-test case titles of a run, for the "What happened" summary."""
    return list(
        test_run.pivot_entries.filter(status=TestRunTestCaseStatus.NEEDS_RETEST)
        .order_by(*RUN_CASE_ORDERING)
        .values_list("test_case__title", flat=True)[:NARRATIVE_TITLE_LIMIT]
    )


def _finished_case_count(summary: dict[str, int]) -> int:
    """Cases with a settled outcome, a person's Needs re-test mark included."""
    return (
        summary.get("success", 0)
        + summary.get("failed", 0)
        + summary.get("blocked", 0)
        + summary.get("needs_retest", 0)
    )


def _build_narrative_for_active_run(test_run: TestRun, summary: dict[str, int]) -> str:
    total = summary.get("total", 0)
    finished = _finished_case_count(summary)
    if test_run.status == TestRunStatus.CANCELLED:
        return f"Run cancelled after {finished} of {total} cases finished."
    if test_run.status == TestRunStatus.PAUSED:
        need_help = summary.get("need_help", 0)
        if not need_help and summary.get("awaiting_person", 0):
            return (
                "Paused: waiting for a person to perform a step. "
                f"{finished} of {total} cases finished so far."
            )
        noun = "case needs" if need_help == 1 else "cases need"
        return (
            f"Paused: {need_help} {noun} help. "
            f"{finished} of {total} cases finished so far."
        )
    return f"{finished} of {total} cases finished so far."


def _build_narrative_for_finished_run(
    summary: dict[str, int],
    failed_titles: list[str],
    blocked_titles: list[str],
    retest_titles: list[str],
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
    retest_count = summary.get("needs_retest", 0)
    if retest_count and retest_titles:
        sentence += (
            f" {retest_count} need re-test: "
            f"{_join_titles(retest_titles, retest_count)}."
        )
    elif retest_count:
        sentence += f" {retest_count} need re-test."
    return sentence


def build_run_narrative(
    test_run: TestRun,
    summary: dict[str, int],
    failed_titles: list[str],
    blocked_titles: list[str],
    retest_titles: Sequence[str] = (),
) -> str:
    """Build the deterministic "What happened" sentence for a run's page.

    ``retest_titles``: `list_retest_case_titles`, queried only when the
    summary counts Needs re-test cases.
    """
    if test_run.status == TestRunStatus.WAITING:
        return (
            f"{summary.get('total', 0)} cases selected. This run has not started yet."
        )
    if test_run.status in (*ACTIVE_RUN_STATUSES, TestRunStatus.CANCELLED):
        return _build_narrative_for_active_run(test_run, summary)
    return _build_narrative_for_finished_run(
        summary, failed_titles, blocked_titles, list(retest_titles)
    )


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
    # A person asked to pause the running case (``pause_pending_for``).
    pause_pending: bool
    # Manual run: the claimed case is paused (the tester's break), and by whom.
    manual_paused: bool
    manual_paused_by: str


class CaseStatusEvent(TypedDict):
    type: str
    status: str
    result: str
    blocked_reason: str
    blocked_reason_label: str
    steps: StepProgressPayload
    pause_pending: bool
    # Who asked to pause; blank unless ``pause_pending``.
    pause_requested_by: str
    # The paused case's discussion page once it opened; blank otherwise (only
    # ``broadcast_case_status`` callers that know it fill it).
    discussion_url: str
    # Manual run: the claimed case is paused (the tester's break), and by whom.
    manual_paused: bool
    manual_paused_by: str


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
        "pause_pending": pause_pending_for(pivot),
        "manual_paused": pivot.manual_paused_at is not None,
        "manual_paused_by": _manual_paused_by(pivot),
    }
    async_to_sync(layer.group_send)(_test_run_group(pivot.test_run_id), event)


def _manual_paused_by(pivot: TestRunTestCase) -> str:
    """Who paused the loaded manual case; blank when it is not paused."""
    return pivot.manual_paused_label if pivot.manual_paused_at is not None else ""


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
    _nudge_runner_after_commit(test_run)


def broadcast_case_status(pivot: TestRunTestCase, *, discussion_url: str = "") -> None:
    """Push the case's status to the run page (pivot event) and the case page.

    ``discussion_url``: the paused case's discussion page, so an open case page
    that showed the pause pending navigates there (``open_paused_help_chat``).
    The pivot needs its pause fields loaded (no query for them here).
    """
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot, discussion_url=discussion_url)


def _broadcast_pivot_status_to_case(
    pivot: TestRunTestCase, *, discussion_url: str = ""
) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    blocked_reason, blocked_reason_label = get_blocked_reason_fields(pivot)
    pause_pending = pause_pending_for(pivot)
    event: CaseStatusEvent = {
        "type": "test_run_case.status",
        "status": pivot.status,
        "result": pivot.result,
        "blocked_reason": blocked_reason,
        "blocked_reason_label": blocked_reason_label,
        "steps": pivot_step_payload(pivot),
        "pause_pending": pause_pending,
        "pause_requested_by": pivot.pause_requested_label if pause_pending else "",
        "discussion_url": discussion_url,
        "manual_paused": pivot.manual_paused_at is not None,
        "manual_paused_by": _manual_paused_by(pivot),
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
            checkpoint_data,
            TestRunTestCaseStatus.IN_PROGRESS,
            pivot.segment_started_at,
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
    _nudge_runner_after_commit(test_run)


def _nudge_runner_after_commit(test_run: TestRun) -> None:
    """Let the Test Runner window re-read its person steps once this commits.

    Every person step transition goes through a run broadcast; some callers
    broadcast inside their transaction (``abort_test_run``), where the
    consumer would still read the old rows. Outside a transaction (including
    inside another ``on_commit`` callback) it runs at once.
    """
    runner_id = runner_id_of_run(test_run)
    if runner_id is not None:
        transaction.on_commit(lambda: nudge_runner_person_steps(runner_id))


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


def _build_cancellation_check(
    test_run_id: int,
    generation: int,
    pivot_id: int | None = None,
    *,
    runner_id: RunnerId | None = None,
) -> Callable[[], bool]:
    """True once the run is cancelled, or resumed/restarted by a newer chain,
    or (with ``runner_id``) no longer on that Test Runner (its owner released
    it), or (with ``pivot_id``) a person marked that case Needs re-test or
    asked to pause it in this generation; still one query per poll."""
    stopped = Q(status=TestRunStatus.CANCELLED) | ~Q(execution_generation=generation)
    if runner_id is not None:
        # A released run has no runner: NULL never equals, so test it too.
        stopped |= Q(runner__isnull=True) | ~Q(runner_id=runner_id)
    if pivot_id is not None:
        stopped |= Q(
            Exists(
                TestRunTestCase.objects.filter(
                    Q(status=TestRunTestCaseStatus.NEEDS_RETEST)
                    | Q(pause_requested_generation=generation),
                    pk=pivot_id,
                )
            )
        )

    def _is_cancelled() -> bool:
        return TestRun.objects.filter(stopped, pk=test_run_id).exists()

    return _is_cancelled


def _build_checkpoint_saved_hook(
    pivot: TestRunTestCase,
) -> Callable[[dict[str, object]], None]:
    """After each landed checkpoint write: close a just-confirmed person step
    (Human in the loop; no query otherwise), then push the case's step rows."""
    settle_person_step = build_person_step_settler(pivot.pk, pivot.execution_checkpoint)

    def _on_saved(data: dict[str, object]) -> None:
        settle_person_step(data)
        _broadcast_case_steps(pivot, data)

    return _on_saved


def _build_person_mode_check(test_run_id: int) -> Callable[[], bool]:
    """True while the run is in Human in the loop (read at each step boundary).

    Read fresh every time, so a mid-run mode switch applies from the next step.
    """

    def _is_person_mode() -> bool:
        return TestRun.objects.filter(
            pk=test_run_id, help_mode=RunHelpMode.HUMAN_IN_THE_LOOP
        ).exists()

    return _is_person_mode


def _is_stale_execution(test_run_id: int, generation: int) -> bool:
    """A newer Start or Resume took over the run; this worker must not write."""
    return not TestRun.objects.filter(
        pk=test_run_id, execution_generation=generation
    ).exists()


def _still_on_runner(test_run_id: int, runner_id: RunnerId) -> bool:
    """The run still has the runner this worker drives (not released)."""
    return TestRun.objects.filter(pk=test_run_id, runner_id=runner_id).exists()


# The worker found its run without a Test Runner, or the owner released it.
_RUNNER_RELEASED_MESSAGE: Final = (
    "The Test Runner was released from this run. Choose a Test Runner, then "
    "continue the case."
)


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
    # Read once at the start: a released or switched runner stops this
    # execution at its next cancellation check (``_build_cancellation_check``).
    runner = pivot.test_run.runner
    runner_id = runner.runner_id if runner is not None else None
    cancellation_check = _build_cancellation_check(
        run_id, generation, pivot.pk, runner_id=runner_id
    )
    # A worker shutdown (BaseException) must not start an agent on its way out.
    allow_teardown = True
    # A case paused for help (or for a person's step, Human in the loop) keeps
    # the machine exactly as it is: no teardown, no cleanup, so a person (or
    # the auto-fix agent) sees what went wrong or performs the step.
    paused_for_help = False
    # Continuing after help, or after a person performed a step: the machine
    # was changed by hand and must not be cleaned up (or restored) before the
    # step re-runs or is confirmed.
    checkpoint_help = read_checkpoint_help(pivot.execution_checkpoint)
    checkpoint_person = read_checkpoint_person_step(pivot.execution_checkpoint)
    continue_after_help = (
        checkpoint_help is not None and checkpoint_help.continue_pending
    ) or (checkpoint_person is not None and checkpoint_person.continue_pending)
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
    if not _mark_pivot_in_progress(pivot, generation):
        # Marked Needs re-test before it was picked up: skip it, but a
        # skipped last case still finishes the run.
        logger.info("Skipping pivot %d marked for re-test", pivot_id)
        _update_test_run_status_if_needed(pivot.test_run, generation=generation)
        return
    # A pause requested in this generation before the pick-up: a redelivered
    # task after the worker died while pausing. It pauses at the first check;
    # the machine is kept as it is.
    pause_redelivered = pivot.pause_requested_generation == generation

    mark = person_mark_verdict(checkpoint_person)
    if mark is not None:
        # A person marked the case failed or blocked: record evidence, then
        # write their verdict (no orchestrator, teardown or cleanup).
        try:
            _finish_person_mark(
                pivot, project, runner, mark, generation, cancellation_check
            )
        finally:
            if not _is_stale_execution(run_id, generation):
                _update_test_run_status_if_needed(pivot.test_run, generation=generation)
        return

    try:
        if runner is None or runner_id is None:
            # Released (or never assigned, a run carried over from before
            # user runners): ask for help like a lost runner.
            raise TestRunnerDisconnectedError(_RUNNER_RELEASED_MESSAGE)
        wait_for_controller(runner_id)

        wait_for_ready_worker(
            timeout=float(settings.VISION_WORKER_READY_TIMEOUT),
            cancellation_check=cancellation_check,
        )

        if not continue_after_help and not pause_redelivered:
            _safe_cleanup(runner_id, wait_for_runner=True)

        on_log = _build_log_callback(pivot, generation)
        on_screenshot = _build_screenshot_callback(pivot, generation)
        task_description = build_task_description(pivot.test_case)
        case_step_count = numbered_case_step_count(pivot.test_case)
        case_steps_testrail = bool(pivot.test_case.testrail_id)

        project.refresh_from_db(
            fields=["project_prompt", "application_url", "application_platform"]
        )
        # A resumed case keeps its log and continues the persisted plan.
        if resume is not None:
            on_log(f"=== Resumed from step {resume.next_step} ===")
        # Built once per execution: it reads the case's open person step.
        checkpoint_saved_hook = _build_checkpoint_saved_hook(pivot)
        # Only a fresh plan reads the artifacts profile (2-3 queries) and the
        # knowledge base index (one query); a resume continues the saved plan.
        artifacts_profile = (
            artifacts_profile_text(project.id) if resume is None else None
        )
        knowledge_index = knowledge_index_text(project.id) if resume is None else None
        while True:
            result = run_orchestrator(
                task_description,
                project.id,
                runner_id,
                on_log=on_log,
                on_screenshot=on_screenshot,
                system_info=runner.system_info or None,
                project_prompt=build_agent_project_context(project),
                cancellation_check=cancellation_check,
                resume=resume,
                on_checkpoint=build_checkpoint_callback(
                    pivot,
                    generation,
                    on_saved=checkpoint_saved_hook,
                ),
                person_mode=_build_person_mode_check(run_id),
                case_step_count=case_step_count,
                case_steps_testrail=case_steps_testrail,
                knowledge_index=knowledge_index,
                artifacts_profile=artifacts_profile,
            )
            # Once a Resume or Start took the run over, the pivot belongs to
            # the newer chain: this worker records nothing (here and below).
            if _is_stale_execution(run_id, generation):
                break
            if result.stop_reason == AgentStopReason.AWAITING_PERSON:
                outcome = _pause_run_for_person(
                    pivot, _person_hand_off(pivot, result), generation
                )
                if outcome == _PersonPause.HANDED_BACK:
                    # The run left Human in the loop before it paused: the
                    # agent performs the step now, from the current screen.
                    resume = load_resume_checkpoint(pivot)
                    continue
                paused_for_help = outcome == _PersonPause.PAUSED
            elif result.stop_reason == AgentStopReason.NEEDS_HUMAN_HELP:
                settle_person_steps(
                    pivot.pk, load_resume_checkpoint(pivot), abandoned="unreferenced"
                )
                paused_for_help = _pause_run_for_help(
                    pivot, _help_request_info(pivot, result), generation
                )
            else:
                settle_person_steps(
                    pivot.pk, load_resume_checkpoint(pivot), abandoned="all"
                )
                # Only the agent's verdict notifies, and only when it was
                # written (not after a re-test mark or an abort); the entry
                # point re-checks the generation, since _finalize_pivot holds
                # no run lock.
                if (
                    _finalize_pivot(pivot, result)
                    and pivot.status == TestRunTestCaseStatus.FAILED
                ):
                    notify_case_failed(pivot.pk, generation=generation)
            break
    except (AgentCancelledError, ControllerNotConnectedError):
        # Inside a case, wait_for_controller only reports "not connected" once
        # the run is no longer STARTED, i.e. it was aborted. A cancel can also
        # be this case's Needs re-test mark: the run goes on, so the mark is
        # kept, no teardown runs (not Failed) and the chain continues below.
        # Or a person's pause: the case asks for help (no teardown, no
        # cleanup) and the discussion opens.
        logger.info("Pivot %d cancelled", pivot_id)
        if not _is_stale_execution(run_id, generation):
            pivot.refresh_from_db(fields=["status", "pause_requested_generation"])
            if pivot.pause_requested_generation == generation:
                if pivot.status == TestRunTestCaseStatus.IN_PROGRESS:
                    paused_for_help = _pause_case_for_person(
                        pivot, generation, restore_baseline
                    )
                else:
                    _discard_pause_after_retest(pivot)
            elif runner_id is not None and not _still_on_runner(run_id, runner_id):
                # The owner released the runner (``run_runner_release``): the
                # case asks for help with the runner-lost fault.
                paused_for_help = _pause_case_after_fault(
                    pivot,
                    TestRunnerDisconnectedError(_RUNNER_RELEASED_MESSAGE),
                    generation,
                    restore_baseline,
                )
            if not paused_for_help:
                _cancel_pivot_if_in_progress(pivot)
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
        # A newer chain may already be driving the machine, or the runner
        # was released to someone else: leave it alone. Checked again after
        # the teardown, which can take minutes.
        if (
            allow_teardown
            and not paused_for_help
            and runner is not None
            and runner_id is not None
            and not _is_stale_execution(run_id, generation)
            and _still_on_runner(run_id, runner_id)
        ):
            _safe_teardown(
                pivot,
                project,
                runner,
                generation=generation,
                # Run-level only: a re-test mark during the teardown of a
                # Failed verdict must not leave its apps open.
                cancellation_check=_build_cancellation_check(
                    run_id, generation, runner_id=runner_id
                ),
                run_case_teardown=run_case_teardown,
            )
        if not _is_stale_execution(run_id, generation):
            if (
                not paused_for_help
                and runner_id is not None
                and _still_on_runner(run_id, runner_id)
            ):
                _safe_cleanup(runner_id, wait_for_runner=False)
            _update_test_run_status_if_needed(pivot.test_run, generation=generation)


def _finish_person_mark(
    pivot: TestRunTestCase,
    project: Project,
    runner: TestRunner | None,
    mark: PersonMarkVerdict,
    generation: int,
    cancellation_check: Callable[[], bool],
) -> None:
    """The evidence pass of a person's Mark failed / Mark blocked, then the
    verdict. Any fault keeps the person's verdict and logs "no evidence"; it
    never opens Need help."""
    from agents.services.person_mark import (  # local import: breaks circular dependency
        record_person_mark,
    )

    on_log = _build_log_callback(pivot, generation)
    checkpoint = load_resume_checkpoint(pivot)
    # Read from the key itself: it also names the row when the rest of the
    # checkpoint cannot be used.
    person = read_checkpoint_person_step(pivot.execution_checkpoint)
    person_step_id = person.request_id if person is not None else None
    recorded: OrchestratorCheckpoint | None = None
    if checkpoint is None:
        on_log("[Evidence] no evidence: the case has no usable saved progress")
    elif runner is None:
        on_log("[Evidence] no evidence: the run has no Test Runner")
        recorded = replace(checkpoint, person=None)
    else:
        project.refresh_from_db(
            fields=["project_prompt", "application_url", "application_platform"]
        )
        try:
            # A runner that is reconnecting gets the usual grace period.
            wait_for_controller(runner.runner_id)
            recorded = record_person_mark(
                checkpoint,
                project.id,
                runner.runner_id,
                on_log=on_log,
                on_screenshot=_build_screenshot_callback(pivot, generation),
                system_info=runner.system_info or None,
                project_prompt=build_agent_project_context(project),
                cancellation_check=cancellation_check,
            )
        except AgentCancelledError:
            on_log("[Evidence] no evidence: the case was stopped")
        except ControllerNotConnectedError:
            on_log("[Evidence] no evidence: the Test Runner is not connected")
        except Exception as exc:
            reason = classify_case_fault(exc)
            logger.warning(
                "Pivot %d: no evidence for the person's mark (%s): %s",
                pivot.pk,
                reason.value,
                exc,
            )
            on_log(f"[Evidence] no evidence: {reason.label}")
        if recorded is None:
            recorded = replace(checkpoint, person=None)
    if _is_stale_execution(pivot.test_run_id, generation):
        return
    if apply_person_mark(
        pivot,
        mark,
        recorded,
        person_step_id=person_step_id,
        generation=generation,
    ):
        _broadcast_pivot_status_to_run(pivot)
        _broadcast_pivot_status_to_case(pivot)


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
        _cancel_pivot_if_in_progress(pivot)
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


def _pause_case_for_person(
    pivot: TestRunTestCase, generation: int, restore_baseline: int | None
) -> bool:
    """Ask for help after a person's pause stopped the case; True when it did.

    Shaped like ``_pause_case_after_fault``: the step is the checkpoint's
    cursor step (clamped to the plan), and the stored continue mode is from
    scratch without a checkpoint, restore while a due restore has not
    finished, otherwise the current screen. ``_pause_run_for_help`` writes
    the pause texts from the request it reads under the run lock.
    """
    checkpoint = load_resume_checkpoint(pivot)
    step_num = (
        min(checkpoint.next_step, max(len(checkpoint.plan), 1))
        if checkpoint is not None
        else 1
    )
    return _pause_run_for_help(
        pivot,
        # Reason and ask are replaced by the pause texts.
        HelpRequestInfo(step_num=step_num, reason="", ask=pause_ask()),
        generation,
        continue_mode=pause_continue_mode(
            checkpoint,
            restore_pending=(
                restore_baseline is not None
                and checkpoint is not None
                and _restore_attempt_count(checkpoint) == restore_baseline
            ),
        ),
        stopped_by_pause=True,
    )


def _discard_pause_after_retest(pivot: TestRunTestCase) -> None:
    """A Needs re-test mark beat a pending pause: drop the request.

    One conditional UPDATE (the mark keeps the case; only its pause fields
    change). The message is lost: the person asked to re-test the case.
    """
    TestRunTestCase.objects.filter(
        pk=pivot.pk, status=TestRunTestCaseStatus.NEEDS_RETEST
    ).update(**PAUSE_REQUEST_CLEARED)
    clear_pause_request(pivot)


def _fetch_pivot(pivot_id: int) -> TestRunTestCase:
    return TestRunTestCase.objects.select_related(
        "test_run__project", "test_run__runner", "test_case"
    ).get(pk=pivot_id)


def _mark_pivot_in_progress(pivot: TestRunTestCase, generation: int) -> bool:
    """Start the case; False (nothing written) when it is marked Needs re-test.

    The case row is locked, so a mark that committed after the worker read the
    case is still seen; a mark that comes later stops the case at its next
    cancellation check. A pause request (or a dropped one's record) of
    another generation is cleared; one of ``generation`` is kept, so a
    redelivered task still pauses. ``pivot.pause_requested_generation`` is
    the locked value afterwards.
    """
    with transaction.atomic():
        locked_status, locked_pause_generation = (
            TestRunTestCase.objects.select_for_update()
            .values_list("status", "pause_requested_generation")
            .get(pk=pivot.pk)
        )
        if locked_status == TestRunTestCaseStatus.NEEDS_RETEST:
            pivot.status = TestRunTestCaseStatus.NEEDS_RETEST
            return False
        pivot.status = TestRunTestCaseStatus.IN_PROGRESS
        # Keeps the first start and the time of earlier segments (Continue,
        # Resume).
        open_case_segment(pivot, timezone.now())
        update_fields = ["status", *CASE_SEGMENT_OPEN_FIELDS, "updated_at"]
        if (
            locked_pause_generation is not None
            and locked_pause_generation != generation
        ):
            clear_pause_request(pivot)
            update_fields.extend(PAUSE_REQUEST_FIELDS)
        elif locked_pause_generation != pivot.pause_requested_generation:
            # Changed since the worker read the case (rare): the broadcast
            # below shows the pauser.
            pivot.refresh_from_db(fields=list(PAUSE_REQUEST_FIELDS))
        pivot.save(update_fields=update_fields)

    # Outside the case lock: markers lock the run before the case.
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
    return True


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
) -> AttemptScreenshotCallback:
    def _save_screenshot(
        base64_data: str, tool_name: str, *, attempt_id: str, step_num: int
    ) -> None:
        if _is_stale_execution(pivot.test_run_id, generation):
            return
        image_bytes = _decode_base64_image(base64_data)
        filename = _generate_screenshot_filename(tool_name)
        _persist_screenshot(
            pivot,
            image_bytes,
            filename,
            tool_name,
            attempt_id=attempt_id,
            step_num=step_num,
        )

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
    *,
    attempt_id: str,
    step_num: int,
) -> None:
    screenshot = TestRunScreenshot.objects.create(
        test_run_test_case=pivot,
        image=ContentFile(image_bytes, name=filename),
        tool_name=tool_name,
        attempt_id=attempt_id,
        step_num=step_num,
    )
    _broadcast_screenshot(pivot, screenshot)


def _finalize_pivot(pivot: TestRunTestCase, result: AgentResult) -> bool:
    """Write the agent's verdict; True when it did.

    Only while the locked case is still In progress: a person's Needs re-test
    mark or the run's abort that landed first is kept. A pending pause is
    dropped in the same save (the verdict won): one with a first message is
    kept as the "Too late to pause" record (no longer pending, since the case
    left In progress), any other is cleared.
    """
    with transaction.atomic():
        locked_status, locked_pause_generation, locked_pause_message = (
            TestRunTestCase.objects.select_for_update()
            .values_list("status", "pause_requested_generation", "pause_message")
            .get(pk=pivot.pk)
        )
        if locked_status != TestRunTestCaseStatus.IN_PROGRESS:
            logger.info(
                "Verdict for pivot %d dropped: it is %s", pivot.pk, locked_status
            )
            pivot.status = locked_status
            return False
        if result.stop_reason == AgentStopReason.CANCELLED:
            pivot.status = TestRunTestCaseStatus.CANCELLED
        elif result.stop_reason == AgentStopReason.TASK_COMPLETE:
            pivot.status = TestRunTestCaseStatus.SUCCESS
        else:
            pivot.status = TestRunTestCaseStatus.FAILED
            pivot.failed_by = FailedBy.AGENT

        now = timezone.now()
        pivot.result = _extract_agent_summary(result)
        pivot.finished_at = now
        update_fields = ["status", "result", "finished_at", "failed_by", "updated_at"]
        if locked_pause_generation is not None and not locked_pause_message:
            clear_pause_request(pivot)
            update_fields.extend(PAUSE_REQUEST_FIELDS)
        pivot.save(update_fields=update_fields)
        # The segment ends at the verdict; the teardown that follows is not
        # counted.
        close_case_segment(pivot.pk, now)
    pivot.segment_started_at = None
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)
    return True


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
    stopped_by_pause: bool = False,
) -> bool:
    """Put the case in Need help and the run to sleep; True when it did.

    ``fault`` is set when an infrastructure fault (not the orchestrator)
    stopped the case; the Night auto-fix decision is the same either way.
    ``continue_mode`` is stored on the request and drives Continue.
    ``stopped_by_pause``: a person's pause stopped the case
    (``_pause_case_for_person``).

    One transaction under the run lock: a stale generation does nothing, and
    a run aborted meanwhile cancels the case instead. The case keeps its
    checkpoint and the machine is left as it is. After commit the run page is
    told, then either the Night auto-fix is enqueued (by task name: importing
    ``machine_setup.tasks`` here would be an import cycle) or the project
    members are notified.

    A pause request of this generation on the case (read under the run lock,
    which ``request_case_pause`` takes too) means a person is there: the
    request records the pauser and the first message
    (``open_pause_help_request``), with the pause texts for a pure pause and
    this call's own texts, fault and mode when the agent or a fault stopped
    the case first. No auto-fix and no ntfy; after commit the discussion is
    opened (``enqueue_open_paused_help_chat``).
    """
    reason = _truncate_help_text(help.reason)
    ask = _truncate_help_text(help.ask) or _HELP_FALLBACK_ASK
    step_num = max(help.step_num, 1)
    with transaction.atomic():
        test_run = TestRun.objects.select_for_update().get(pk=pivot.test_run_id)
        if test_run.execution_generation != generation:
            return False
        pivot.refresh_from_db(fields=["status", *PAUSE_REQUEST_FIELDS])
        if test_run.status != TestRunStatus.STARTED:
            if pivot.status == TestRunTestCaseStatus.IN_PROGRESS:
                _mark_pivot_cancelled(pivot)
            return False
        if pivot.status != TestRunTestCaseStatus.IN_PROGRESS:
            return False
        pause = read_pause_request(pivot, generation)
        if stopped_by_pause and pause is None:
            # Unreachable in practice: only this generation's pick-up, its
            # verdict and a cancel clear a request while the case runs. With
            # no pauser to name, the caller cancels the case as for any
            # other stop.
            logger.warning("Pivot %d paused without a pause request", pivot.pk)
            return False

        auto_fix = (
            pause is None
            and test_run.help_mode == RunHelpMode.NIGHT
            and auto_fix_eligible(pivot.pk, step_num)
        )
        now = timezone.now()
        # The worker stops owning the case: waiting for help is not counted.
        close_case_segment(pivot.pk, now)
        pivot.segment_started_at = None
        update_fields = [
            "status",
            "blocked_reason",
            "result",
            "finished_at",
            "updated_at",
        ]
        if pause is not None:
            help_request = open_pause_help_request(
                pivot,
                step_num=step_num,
                continue_mode=continue_mode,
                requested=pause,
                stopped_by_pause=stopped_by_pause,
                base=HelpRequestInfo(step_num=step_num, reason=reason, ask=ask),
                now=now,
                fault=fault or "",
            )
            ask = help_request.ask
            update_fields.extend(PAUSE_REQUEST_FIELDS)
        else:
            # At most one open request per case: close a leftover before
            # opening.
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
        pivot.save(update_fields=update_fields)
        test_run.status = TestRunStatus.PAUSED
        test_run.save(update_fields=["status", "updated_at"])

        if stopped_by_pause and pause is not None:
            log_line = pause_log_line(pause.label, step_num)
        elif fault is None:
            log_line = f"[Need help at step {step_num}] {ask}"
        else:
            log_line = f"[Need help: {fault.label} at step {step_num}] {ask}"
        if auto_fix:
            log_line += " The machine setup agent will try to fix it."
        logged = _append_pivot_log(pivot.pk, log_line, generation)
        help_request_id = help_request.pk
        project_id = test_run.project_id
        paused_by_person = pause is not None

        def _after_commit() -> None:
            if logged:
                _broadcast_log(pivot, log_line)
            broadcast_run_and_pivots(test_run, [pivot])
            broadcast_project_attention_changed(project_id)
            if paused_by_person:
                enqueue_open_paused_help_chat(help_request_id)
            elif auto_fix:
                enqueue_case_auto_fix(help_request_id)
            else:
                notify_case_needs_help(help_request_id)

        transaction.on_commit(_after_commit)
    return True


# The case result while a step waits for a person: the instruction's head.
_PERSON_RESULT_HEAD_MAX = 300


def _person_hand_off(pivot: TestRunTestCase, result: AgentResult) -> PersonStepHandOff:
    """The orchestrator's hand-off; the checkpoint's when the result lacks it.

    Raises when neither has one: the worker then asks for help (internal error).
    """
    if result.person_step is not None:
        return result.person_step
    person = read_checkpoint_person_step(pivot.execution_checkpoint)
    if person is None:
        raise RuntimeError(f"Pivot {pivot.pk} awaits a person without a hand-off")
    logger.warning("Pivot %d awaits a person without a hand-off result", pivot.pk)
    return person.hand_off


class _PersonPause(StrEnum):
    """What ``_pause_run_for_person`` did."""

    # The case waits for the person; the run is PAUSED.
    PAUSED = "paused"
    # Nothing (stale generation, run aborted, case no longer In progress).
    SKIPPED = "skipped"
    # The run left Human in the loop before the pause: the caller runs the
    # orchestrator again and the agent performs the step.
    HANDED_BACK = "handed_back"


def _pause_run_for_person(
    pivot: TestRunTestCase, hand_off: PersonStepHandOff, generation: int
) -> _PersonPause:
    """Hand the step to a person and put the run to sleep.

    Human in the loop. Mirrors ``_pause_run_for_help``: one transaction under
    the run lock; a stale generation does nothing and a run aborted meanwhile
    cancels the case. A person step the checkpoint save hook did not settle is
    settled (backstop), the open ``CasePersonStep`` is created (or reopened as not
    confirmed), its id is written into the checkpoint ``person`` key, the case
    waits for the person (``AWAITING_PERSON``) and the run is PAUSED. The
    machine is left as it is. After commit the pages and the attention inbox
    are told, and the members are notified when the hand-off starts a new
    sequence of person steps (``open_person_step_row``) and on every
    not-confirmed round.

    The mode is read again under the run lock: a switch away from Human in
    the loop after the orchestrator's last ``person_mode()`` check and before
    this lock found the run Started, so ``change_run_help_mode`` handed
    nothing back. Pausing would leave a step no one answers in Attended or
    Night, so the step is handed back here instead (``_hand_back_unpaused``)
    and the caller re-runs the orchestrator in the same execution. Re-queueing
    the case is not an option: ``run_reopen_services`` imports this module.

    A person's pause of this generation that raced the hand-off wins: the
    case asks for help instead (``_pause_hand_off_for_person``).
    """
    step_num = max(hand_off.step_num, 1)
    # The run's Test Runner OS names the shell (loaded with the case).
    runner = pivot.test_run.runner
    shell_label = command_shell_label(
        hand_off.kind, runner.system_info if runner is not None else None
    )
    with transaction.atomic():
        test_run = TestRun.objects.select_for_update().get(pk=pivot.test_run_id)
        if test_run.execution_generation != generation:
            return _PersonPause.SKIPPED
        pivot.refresh_from_db(
            fields=["status", "execution_checkpoint", *PAUSE_REQUEST_FIELDS]
        )
        if test_run.status != TestRunStatus.STARTED:
            if pivot.status == TestRunTestCaseStatus.IN_PROGRESS:
                _mark_pivot_cancelled(pivot)
            return _PersonPause.SKIPPED
        if pivot.status != TestRunTestCaseStatus.IN_PROGRESS:
            return _PersonPause.SKIPPED
        if test_run.help_mode != RunHelpMode.HUMAN_IN_THE_LOOP:
            _hand_back_unpaused(pivot, hand_off, generation)
            return _PersonPause.HANDED_BACK

        now = timezone.now()
        segment_started_at = pivot.segment_started_at
        # The worker stops owning the case: the person's time is not counted.
        close_case_segment(pivot.pk, now)
        pivot.segment_started_at = None
        checkpoint = load_resume_checkpoint(pivot)
        settle_person_steps(pivot.pk, checkpoint)
        pause = read_pause_request(pivot, generation)
        if pause is not None:
            _pause_hand_off_for_person(
                test_run, pivot, hand_off, pause, generation=generation, now=now
            )
            return _PersonPause.PAUSED
        person_step, push = open_person_step_row(
            pivot.pk,
            hand_off,
            shell_label=shell_label,
            case_steps=case_step_rows(
                pivot.test_case,
                hand_off.case_steps,
                case_step_count=checkpoint.case_step_count if checkpoint else 0,
            ),
            case_steps_label=case_steps_label(
                hand_off.case_steps,
                testrail=checkpoint.case_steps_testrail if checkpoint else True,
            ),
            segment_started_at=segment_started_at,
            now=now,
        )

        pivot.execution_checkpoint = with_person_request_id(
            pivot.execution_checkpoint, hand_off, person_step.pk, now
        )
        pivot.status = TestRunTestCaseStatus.AWAITING_PERSON
        pivot.blocked_reason = ""
        pivot.result = cap_text(
            " ".join(person_step.instruction.split()), _PERSON_RESULT_HEAD_MAX
        )
        pivot.finished_at = None
        pivot.save(
            update_fields=[
                "status",
                "blocked_reason",
                "result",
                "finished_at",
                "execution_checkpoint",
                "updated_at",
            ]
        )
        test_run.status = TestRunStatus.PAUSED
        test_run.save(update_fields=["status", "updated_at"])

        if person_step.status == PersonStepStatus.NOT_CONFIRMED:
            log_line = (
                f"[Not confirmed at step {step_num}] {person_step.agent_objection} "
                "Waiting for the person again."
            )
        else:
            log_line = f"[Waiting for a person at step {step_num}] {pivot.result}"
        logged = _append_pivot_log(pivot.pk, log_line, generation)
        person_step_id = person_step.pk
        project_id = test_run.project_id

        def _after_commit() -> None:
            if logged:
                _broadcast_log(pivot, log_line)
            broadcast_run_and_pivots(test_run, [pivot])
            broadcast_project_attention_changed(project_id)
            if push:
                notify_person_step(person_step_id)

        transaction.on_commit(_after_commit)
    return _PersonPause.PAUSED


def _pause_hand_off_for_person(
    test_run: TestRun,
    pivot: TestRunTestCase,
    hand_off: PersonStepHandOff,
    pause: PauseRequestSnapshot,
    *,
    generation: int,
    now: datetime,
) -> None:
    """Open a person's pause instead of handing the step to a person.

    The caller holds the run lock, the run is Started in Human in the loop
    and the case In progress, its segment closed. An open person step row
    (a report the agent could not confirm) is cancelled as paused by the
    pauser. The checkpoint keeps the prepared hand-off as a ``waiting``
    ``person`` key with no row, so a current-screen Continue hands the same
    step out again. The case needs help (current screen) and the run is
    PAUSED; after commit the pages are told and the discussion is opened.
    """
    step_num = max(hand_off.step_num, 1)
    CasePersonStep.objects.filter(
        test_run_test_case_id=pivot.pk, status__in=OPEN_PERSON_STEP_STATUSES
    ).update(
        status=PersonStepStatus.CANCELLED,
        close_reason=PersonStepClose.PAUSED,
        closed_by_id=pause.user_id,
        closed_via=pause.via,
        closed_at=now,
    )
    person = read_checkpoint_person_step(pivot.execution_checkpoint)
    pivot.execution_checkpoint = with_person_step(
        pivot.execution_checkpoint,
        CheckpointPersonStep(
            hand_off=hand_off,
            request_id=None,
            state="waiting",
            requested_at=(
                person.requested_at
                if person is not None and person.hand_off.step_num == step_num
                else now
            ),
        ),
    )
    help_request = open_pause_help_request(
        pivot,
        step_num=step_num,
        continue_mode=HelpContinueMode.CURRENT_SCREEN,
        requested=pause,
        stopped_by_pause=True,
        base=None,
        now=now,
    )
    mark_case_paused_for_help(pivot, help_request)
    pivot.save(update_fields=["execution_checkpoint", *PAUSED_CASE_FIELDS])
    test_run.status = TestRunStatus.PAUSED
    test_run.save(update_fields=["status", "updated_at"])

    log_line = pause_log_line(pause.label, step_num)
    logged = _append_pivot_log(pivot.pk, log_line, generation)
    help_request_id = help_request.pk
    project_id = test_run.project_id

    def _after_commit() -> None:
        if logged:
            _broadcast_log(pivot, log_line)
        broadcast_run_and_pivots(test_run, [pivot])
        broadcast_project_attention_changed(project_id)
        enqueue_open_paused_help_chat(help_request_id)

    transaction.on_commit(_after_commit)


def _hand_back_unpaused(
    pivot: TestRunTestCase, hand_off: PersonStepHandOff, generation: int
) -> None:
    """Give the agent a step handed out after the run left Human in the loop.

    The caller holds the run lock, the run is Started and the case In
    progress (it stays so, its segment open). A row still open for the case
    (a report the agent could not confirm) is closed as handed back, and the
    checkpoint ``person`` key becomes ``handed_back`` with ``continue_pending``,
    so the orchestrator's re-entry skips the restore and the agent runs the
    step from the current screen.
    """
    now = timezone.now()
    settle_person_steps(pivot.pk, load_resume_checkpoint(pivot))
    open_id = (
        CasePersonStep.objects.filter(
            test_run_test_case_id=pivot.pk, status__in=OPEN_PERSON_STEP_STATUSES
        )
        .values_list("pk", flat=True)
        .first()
    )
    if open_id is not None:
        hand_back_person_step_row(pivot.pk, open_id, now)
    person = read_checkpoint_person_step(pivot.execution_checkpoint)
    if person is None or person.hand_off.step_num != hand_off.step_num:
        person = CheckpointPersonStep(
            hand_off=hand_off, request_id=None, state="waiting", requested_at=now
        )
    pivot.execution_checkpoint = with_person_step(
        pivot.execution_checkpoint,
        replace(person, request_id=open_id, state="handed_back", continue_pending=True),
    )
    pivot.save(update_fields=["execution_checkpoint", "updated_at"])
    step_num = max(hand_off.step_num, 1)
    log_line = (
        f"[Human in the loop is off at step {step_num}] The agent performs the "
        "step itself."
    )
    if _append_pivot_log(pivot.pk, log_line, generation):
        transaction.on_commit(lambda: _broadcast_log(pivot, log_line))


def _extract_agent_summary(result: AgentResult) -> str:
    for message in reversed(result.messages):
        if message.role == "assistant" and isinstance(message.content, str):
            return message.content
    return result.error or ""


def _cancel_pivot_if_in_progress(pivot: TestRunTestCase) -> None:
    """Cancel the worker's own case, only while it is still In progress.

    One conditional UPDATE: a Needs re-test mark (or an abort, or a verdict)
    that landed first is never overwritten. The segment close is idempotent,
    so a marker that already closed it adds nothing.
    """
    now = timezone.now()
    # A pending pause is discarded with it (the run's abort won).
    cancelled = TestRunTestCase.objects.filter(
        pk=pivot.pk, status=TestRunTestCaseStatus.IN_PROGRESS
    ).update(
        status=TestRunTestCaseStatus.CANCELLED,
        finished_at=now,
        updated_at=now,
        **PAUSE_REQUEST_CLEARED,
    )
    if not cancelled:
        return
    clear_pause_request(pivot)
    close_case_segment(pivot.pk, now)
    pivot.status = TestRunTestCaseStatus.CANCELLED
    pivot.finished_at = now
    pivot.segment_started_at = None
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)


def _mark_pivot_cancelled(pivot: TestRunTestCase) -> None:
    now = timezone.now()
    pivot.status = TestRunTestCaseStatus.CANCELLED
    pivot.finished_at = now
    # A pending pause is discarded with the case (abort won).
    clear_pause_request(pivot)
    pivot.save(
        update_fields=["status", "finished_at", *PAUSE_REQUEST_FIELDS, "updated_at"]
    )
    # Need help, Waiting for a person and Created cases have no open segment:
    # skip the query (abort cancels many). The close itself is idempotent when
    # an abort races the worker's own cancel; that worker's pivot still shows
    # its segment.
    if pivot.segment_started_at is not None:
        close_case_segment(pivot.pk, now)
        pivot.segment_started_at = None
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

    # Help requests and person steps before cases: the lock order of every
    # help and person step writer.
    _cancel_run_help_requests(test_run, now)
    # A person's Mark failed / blocked still waiting for its evidence pass
    # keeps the person's verdict.
    for marked in settle_person_marks_on_abort(test_run.pk, now):
        _broadcast_pivot_status_to_run(marked)
        _broadcast_pivot_status_to_case(marked)
    _cancel_run_person_steps(test_run, now)

    # A Need help (or person step) case is Cancelled like a running one:
    # Resume continues it.
    for pivot in test_run.pivot_entries.filter(
        status__in=[
            TestRunTestCaseStatus.IN_PROGRESS,
            TestRunTestCaseStatus.NEED_HELP,
            TestRunTestCaseStatus.AWAITING_PERSON,
        ]
    ):
        _mark_pivot_cancelled(pivot)

    for pivot in test_run.pivot_entries.filter(status=TestRunTestCaseStatus.CREATED):
        _mark_pivot_cancelled(pivot)

    # Manual run: the claimed case was cancelled above; its claim goes too, so
    # a late hand-in is dropped. Hand-ins already made keep uploading.
    test_run.pivot_entries.exclude(manual_claim_token="").update(**MANUAL_CLAIM_CLEARED)

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
    cancelled = open_requests.update(
        resolution=HelpResolution.CANCELLED,
        resolved_at=now,
        agent_proposal_action=HelpProposalAction.NONE,
        agent_proposal_note="",
        agent_proposal_at=None,
    )
    if cancelled:
        project_id = test_run.project_id
        transaction.on_commit(lambda: broadcast_project_attention_changed(project_id))
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


def _cancel_run_person_steps(test_run: TestRun, now: datetime) -> None:
    """Close the run's open person steps (Human in the loop) as cancelled."""
    cancelled = CasePersonStep.objects.filter(
        test_run_test_case__test_run=test_run, status__in=OPEN_PERSON_STEP_STATUSES
    ).update(status=PersonStepStatus.CANCELLED, closed_at=now)
    if cancelled:
        project_id = test_run.project_id
        transaction.on_commit(lambda: broadcast_project_attention_changed(project_id))


def _update_test_run_status_if_needed(test_run: TestRun, *, generation: int) -> bool:
    """Finish the run once every case settled; True for the one caller that did.

    The STARTED -> DONE write is a conditional UPDATE on the status and
    ``generation``: when two case workers finish together, Postgres re-checks
    the WHERE of the second after the first commits, so exactly one caller per
    generation finishes the run and sends the run-finished notification (on
    commit). A worker of an older generation matches nothing.
    """
    test_run.refresh_from_db()
    # A paused run finishes only after its Need help case is resolved (or
    # its person step is reported).
    if test_run.status in (TestRunStatus.CANCELLED, TestRunStatus.PAUSED):
        return False

    all_pivots = test_run.pivot_entries.all()

    if not all_pivots.exists():
        return False

    all_done = not all_pivots.filter(
        status__in=[
            TestRunTestCaseStatus.CREATED,
            TestRunTestCaseStatus.IN_PROGRESS,
            TestRunTestCaseStatus.NEED_HELP,
            TestRunTestCaseStatus.AWAITING_PERSON,
        ]
    ).exists()

    if not all_done:
        return False
    if not finish_test_run(
        test_run, from_status=TestRunStatus.STARTED, generation=generation
    ):
        return False
    broadcast_test_run_status(test_run)
    return True


def finish_test_run(
    test_run: TestRun, *, from_status: TestRunStatus, generation: int
) -> bool:
    """The single point that moves a run to DONE; True when this call did it.

    A conditional UPDATE on ``from_status`` (STARTED, or PAUSED once its last
    Need help case is settled) and ``generation``, so exactly one caller per
    generation finishes the run. Only then are the in-memory fields set and the
    run-finished notification and the members' attention nudge scheduled on
    commit. Status broadcasts stay with the callers.
    """
    now = timezone.now()
    finished = TestRun.objects.filter(
        pk=test_run.pk,
        status=from_status,
        execution_generation=generation,
    ).update(status=TestRunStatus.DONE, finished_at=now, updated_at=now)
    if finished != 1:
        return False
    test_run.status = TestRunStatus.DONE
    test_run.finished_at = now
    test_run.updated_at = now
    run_id = test_run.pk
    project_id = test_run.project_id

    def _after_commit() -> None:
        notify_run_finished(run_id, generation=generation)
        broadcast_project_attention_changed(project_id)

    transaction.on_commit(_after_commit)
    return True
