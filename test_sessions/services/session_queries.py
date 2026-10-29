"""Read side of test sessions: the open session, the list and detail pages.

Every query is filtered on the owner, so another user's session id is "not
found". Pages get frozen dataclasses (the template contract), never ORM
instances, and each function runs a fixed number of queries whatever the
page size.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypedDict, cast

from django.core.paginator import Page, Paginator
from django.db.models import Count, IntegerField, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from projects.models import Project, TestRunTestCaseStatus
from test_sessions.exceptions import SessionNotFoundError
from test_sessions.models import TestSession, TestSessionCase
from test_sessions.services.case_outcome import (
    BLOCKED_REASON,
    DURATION,
    FAILED_BY,
    FINISHED_AT,
    STARTED_AT,
    STATUS,
    actions_label,
    blocked_reason_label,
    effective_duration,
    effective_status,
    failed_by_label,
    outcome_annotations,
    performed_by_label,
    status_label,
)


@dataclass(frozen=True)
class SessionListRow:
    id: int
    display_name: str
    started_at: datetime
    ended_at: datetime | None
    is_open: bool
    closed_automatically: bool
    wall_clock: timedelta
    test_count: int
    project_count: int
    test_time: timedelta | None


@dataclass(frozen=True)
class StatusCount:
    status: str
    label: str
    count: int


@dataclass(frozen=True)
class ProjectBreakdown:
    project_id: int | None
    project_name: str
    test_count: int
    test_time: timedelta | None


@dataclass(frozen=True)
class SessionSummary:
    wall_clock: timedelta
    test_time: timedelta | None
    test_count: int
    project_count: int
    person_step_count: int
    status_counts: list[StatusCount]
    by_project: list[ProjectBreakdown]


@dataclass(frozen=True)
class SessionCaseRow:
    id: int
    pivot_id: int | None
    project_id: int | None
    project_name: str
    run_id: int | None
    run_name: str
    case_title: str
    testrail_id: str
    # "" when not available (the run case is gone and the row was not frozen).
    status: str
    status_label: str
    failed_by_label: str
    blocked_reason_label: str
    started_at: datetime | None
    finished_at: datetime | None
    duration: timedelta | None
    performed_by_label: str
    actions_label: str
    person_step_count: int
    is_live: bool
    case_url: str | None


class _ProjectValues(TypedDict):
    project_id: int | None
    name: str
    test_count: int
    test_time: timedelta | None


class _CaseValues(TypedDict):
    id: int
    test_run_test_case_id: int | None
    pivot_ref: int | None
    project_id: int | None
    project_name: str
    test_run_id: int | None
    run_ref: int | None
    run_name: str
    case_title: str
    testrail_case_id: str
    run_help_mode: str
    first_activity: str
    last_activity: str
    person_step_count: int
    frozen_at: datetime | None
    outcome_status: str | None
    outcome_failed_by: str | None
    outcome_blocked_reason: str | None
    outcome_started_at: datetime | None
    outcome_finished_at: datetime | None
    outcome_duration: timedelta | None


# Summary order of the status breakdown; "" (not available) comes last.
_STATUS_ORDER: list[str] = [str(value) for value in TestRunTestCaseStatus.values]
_NOT_AVAILABLE_KEY = "not_available"


def _wall_clock(started_at: datetime, ended_at: datetime | None) -> timedelta:
    end = ended_at if ended_at is not None else timezone.now()
    return max(timedelta(0), end - started_at)


def run_label(run_name: str, run_ref: int | None) -> str:
    """The run's snapshot name, else "Run #N" like ``TestRun.display_name``."""
    if run_name:
        return run_name
    return f"Run #{run_ref}" if run_ref is not None else ""


def get_open_session(user: CustomUser) -> TestSession | None:
    """The user's open session, if any (1 indexed query)."""
    return TestSession.objects.filter(user=user, ended_at__isnull=True).first()


def get_session_for_user(user: CustomUser, session_id: int) -> TestSession:
    """The user's session ``session_id`` (1 query); ``SessionNotFoundError``
    when it does not exist or belongs to someone else."""
    session = TestSession.objects.filter(user=user, id=session_id).first()
    if session is None:
        raise SessionNotFoundError()
    return session


def list_sessions(
    user: CustomUser, *, page: int, per_page: int
) -> Page[SessionListRow]:
    """One page of the user's sessions, newest first, with their totals
    (paginator count + one page query of correlated subqueries: 2 queries)."""
    cases = TestSessionCase.objects.filter(session=OuterRef("pk")).order_by()
    test_count = cases.values("session").annotate(n=Count("id")).values("n")
    project_count = (
        cases.values("session").annotate(n=Count("project", distinct=True)).values("n")
    )
    test_time = (
        cases.values("session")
        .annotate(total=Sum(effective_duration()))
        .values("total")
    )
    sessions = (
        TestSession.objects.filter(user=user)
        .annotate(
            test_count=Coalesce(Subquery(test_count, output_field=IntegerField()), 0),
            project_count=Coalesce(
                Subquery(project_count, output_field=IntegerField()), 0
            ),
            test_time=Subquery(test_time),
        )
        .order_by("-started_at", "-id")
    )
    paginator = Paginator(sessions, per_page)
    session_page = paginator.get_page(page)
    rows = [
        SessionListRow(
            id=session.pk,
            display_name=session.display_name,
            started_at=session.started_at,
            ended_at=session.ended_at,
            is_open=session.is_open,
            closed_automatically=session.closed_automatically,
            wall_clock=_wall_clock(session.started_at, session.ended_at),
            # Annotations above; django-stubs does not type them on the model.
            test_count=cast(int, getattr(session, "test_count")),
            project_count=cast(int, getattr(session, "project_count")),
            test_time=cast(timedelta | None, getattr(session, "test_time")),
        )
        for session in session_page.object_list
    ]
    # The paginator counted the sessions; this page holds the same rows mapped
    # to SessionListRow, which django-stubs' Page[T] cannot express.
    return Page(rows, session_page.number, paginator)  # type: ignore[arg-type]


def get_session_summary(session: TestSession) -> SessionSummary:
    """Totals, status breakdown and per-project breakdown of ``session``
    (one totals aggregate + one per-project GROUP BY: 2 queries). The caller
    has already checked ownership (``get_session_for_user``)."""
    cases = TestSessionCase.objects.filter(session=session)
    status_aggregates = {
        f"status_{value}": Count("id", filter=Q(eff_status=value))
        for value in _STATUS_ORDER
    }
    totals = cases.annotate(eff_status=effective_status()).aggregate(
        test_count=Count("id"),
        project_count=Count("project", distinct=True),
        test_time=Sum(effective_duration()),
        person_step_count=Coalesce(Sum("person_step_count"), 0),
        **{_NOT_AVAILABLE_KEY: Count("id", filter=Q(eff_status__isnull=True))},
        **status_aggregates,
    )
    status_counts = [
        StatusCount(
            status=value, label=status_label(value), count=totals[f"status_{value}"]
        )
        for value in _STATUS_ORDER
        if totals[f"status_{value}"]
    ]
    if totals[_NOT_AVAILABLE_KEY]:
        status_counts.append(
            StatusCount(
                status="",
                label=status_label(None),
                count=totals[_NOT_AVAILABLE_KEY],
            )
        )
    # Grouped on the project id; the snapshot name may differ between rows if
    # the project was renamed, so the newest-sorting one is shown.
    project_rows = cast(
        list[_ProjectValues],
        list(
            cases.order_by()
            .values("project_id")
            .annotate(
                name=Max("project_name"),
                test_count=Count("id"),
                test_time=Sum(effective_duration()),
            )
            .order_by("-test_count", "name")
        ),
    )
    return SessionSummary(
        wall_clock=_wall_clock(session.started_at, session.ended_at),
        test_time=totals["test_time"],
        test_count=totals["test_count"],
        project_count=totals["project_count"],
        person_step_count=totals["person_step_count"],
        status_counts=status_counts,
        by_project=[
            ProjectBreakdown(
                project_id=row["project_id"],
                project_name=row["name"],
                test_count=row["test_count"],
                test_time=row["test_time"],
            )
            for row in project_rows
        ],
    )


_CASE_FIELDS = (
    "id",
    "test_run_test_case_id",
    "pivot_ref",
    "project_id",
    "project_name",
    "test_run_id",
    "run_ref",
    "run_name",
    "case_title",
    "testrail_case_id",
    "run_help_mode",
    "first_activity",
    "last_activity",
    "person_step_count",
    "frozen_at",
    STATUS,
    FAILED_BY,
    BLOCKED_REASON,
    STARTED_AT,
    FINISHED_AT,
    DURATION,
)


def _case_url(row: _CaseValues, member_project_ids: set[int]) -> str | None:
    project_id = row["project_id"]
    run_id = row["test_run_id"]
    pivot_id = row["test_run_test_case_id"]
    if project_id is None or run_id is None or pivot_id is None:
        return None
    if project_id not in member_project_ids:
        return None
    return reverse(
        "projects:test_run_case_detail",
        kwargs={"project_id": project_id, "test_run_id": run_id, "pivot_id": pivot_id},
    )


def list_session_cases(
    session: TestSession, user: CustomUser, *, page: int, per_page: int
) -> Page[SessionCaseRow]:
    """One page of the session's cases in the order they were first acted on
    (paginator count + one ``values()`` page + the user's member projects
    among them: at most 3 queries). A case links to its page only while the
    user is still a member of its (unarchived) project."""
    cases = (
        TestSessionCase.objects.filter(session=session)
        .annotate(**outcome_annotations())
        .order_by("id")
        .values(*_CASE_FIELDS)
    )
    paginator = Paginator(cases, per_page)
    case_page = paginator.get_page(page)
    # values() rows: the dict shape is fixed by _CASE_FIELDS above.
    values = cast(list[_CaseValues], list(case_page.object_list))
    page_project_ids = {
        row["project_id"] for row in values if row["project_id"] is not None
    }
    member_project_ids: set[int] = set()
    if page_project_ids:
        member_project_ids = set(
            Project.objects.filter(
                id__in=page_project_ids, members=user, archived=False
            ).values_list("id", flat=True)
        )
    rows = [_case_row(row, member_project_ids) for row in values]
    # The paginator counted the dicts; this page holds the same rows mapped to
    # SessionCaseRow (the values() paginator is typed loosely by django-stubs).
    return Page(rows, case_page.number, paginator)


def _case_row(row: _CaseValues, member_project_ids: set[int]) -> SessionCaseRow:
    status = row["outcome_status"] or ""
    pivot_id = row["test_run_test_case_id"]
    run_id = row["test_run_id"]
    return SessionCaseRow(
        id=row["id"],
        pivot_id=pivot_id if pivot_id is not None else row["pivot_ref"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        run_id=run_id if run_id is not None else row["run_ref"],
        run_name=run_label(row["run_name"], row["run_ref"]),
        case_title=row["case_title"],
        testrail_id=row["testrail_case_id"],
        status=status,
        status_label=status_label(status),
        failed_by_label=failed_by_label(status, row["outcome_failed_by"]),
        blocked_reason_label=blocked_reason_label(
            status, row["outcome_blocked_reason"]
        ),
        started_at=row["outcome_started_at"],
        finished_at=row["outcome_finished_at"],
        duration=row["outcome_duration"],
        performed_by_label=performed_by_label(row["run_help_mode"]),
        actions_label=actions_label(row["first_activity"], row["last_activity"]),
        person_step_count=row["person_step_count"],
        is_live=row["frozen_at"] is None,
        case_url=_case_url(row, member_project_ids),
    )
