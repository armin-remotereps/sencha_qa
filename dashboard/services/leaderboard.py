"""The user leaderboard: how many cases each user got to a verdict, the person
steps they performed and the help requests they resolved, in the current and
the previous window, plus two snapshots (Need help now, Remaining).

Visibility is global: every logged-in user sees every eligible user, with bare
counts across all projects (no project names). The project filter is
authorized by the caller's form (it is only ever one of the viewer's
projects); the run-mode filter matches ``TestRun.help_mode``.

Bounded and constant whatever the number of users, projects or cases
(``now`` = the plan's queries 2-9; query 1, the project choices, is the
view's):

1. verdicts per credited user, current and previous window (conditional
   ``Count`` s; the NULL group is the unattributed count);
2. eligible users (``CustomUser`` ``.only(...)``, given the credited ids);
3. reported person steps per reporter, current and previous window;
4. resolved help requests per resolver, current and previous window;
5. active days: one ``UNION`` of verdict days and person-step days in the
   current window (Last 7 days only; skipped for Today);
6. Need help now per credited user (snapshot, non-archived projects);
7. writable memberships of non-archived projects;
8. remaining cases per project (open runs of non-archived projects).

Eight queries for Last 7 days, seven for Today.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from django.db.models import Count, F, Q, QuerySet
from django.db.models.functions import TruncDate
from django.utils import timezone

from accounts.models import CustomUser
from dashboard.leaderboard_rules import POINTS, build_rows, podium, resolve_window
from dashboard.leaderboard_types import (
    Leaderboard,
    LeaderboardFilters,
    LeaderboardPeriod,
    LeaderboardWindow,
    UserInput,
    UserTally,
)
from projects.case_credit import CREDITED_HELP_RESOLUTIONS
from projects.models import (
    FINISHED_CASE_STATUSES,
    WRITABLE_ROLES,
    CaseHelpRequest,
    CasePersonStep,
    ProjectMembership,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)

# Case statuses that count as a result (Cancelled never counts).
COUNTED_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
    TestRunTestCaseStatus.NEEDS_RETEST,
)
WAITING_ON_PERSON: tuple[str, ...] = (
    TestRunTestCaseStatus.NEED_HELP,
    TestRunTestCaseStatus.AWAITING_PERSON,
)
OPEN_RUN_STATUSES: tuple[str, ...] = (
    TestRunStatus.WAITING,
    TestRunStatus.STARTED,
    TestRunStatus.PAUSED,
)

_EMPTY = UserTally()


def get_leaderboard(
    *,
    viewer: CustomUser,
    filters: LeaderboardFilters,
    now: datetime | None = None,
) -> Leaderboard:
    """Rank every eligible user for ``filters`` (seven or eight queries)."""
    window = resolve_window(filters.period, now or timezone.now())

    verdicts, unattributed = _verdicts(filters, window)
    users = _eligible_users(filters, verdicts.keys())
    steps = _person_steps(filters, window)
    helps = _help_resolved(filters, window)
    active_dates = (
        _active_dates(filters, window)
        if filters.period is LeaderboardPeriod.WEEK
        else {}
    )
    need_help = _need_help_now(filters)
    remaining = _remaining(filters)

    inputs = [
        UserInput(
            user_id=user.pk,
            name=user.get_full_name() or user.email,
            current=_tally(
                verdicts.get(user.pk, ({}, {}))[0],
                steps.get(user.pk, (0, 0))[0],
                helps.get(user.pk, (0, 0))[0],
            ),
            previous=_tally(
                verdicts.get(user.pk, ({}, {}))[1],
                steps.get(user.pk, (0, 0))[1],
                helps.get(user.pk, (0, 0))[1],
            ),
            need_help_now=need_help.get(user.pk, 0),
            remaining=remaining.get(user.pk, 0),
            active_dates=frozenset(active_dates.get(user.pk, ())),
        )
        for user in users
    ]
    # Results of users who are not listed (deactivated) are not shown either.
    listed = {user.pk for user in users}
    unlisted = sum(
        sum(current.values())
        for user_id, (current, _previous) in verdicts.items()
        if user_id not in listed
    )
    rows = build_rows(inputs, viewer_id=viewer.pk, window=window)
    return Leaderboard(
        window=window,
        filters=filters,
        rows=rows,
        podium=podium(rows),
        viewer_row=next((row for row in rows if row.is_viewer), None),
        unattributed_count=unattributed + unlisted,
        points=POINTS,
    )


def _tally(statuses: dict[str, int], steps: int, helps: int) -> UserTally:
    if not statuses and not steps and not helps:
        return _EMPTY
    return UserTally(
        passed=statuses.get(TestRunTestCaseStatus.SUCCESS, 0),
        failed=statuses.get(TestRunTestCaseStatus.FAILED, 0),
        blocked=statuses.get(TestRunTestCaseStatus.BLOCKED, 0),
        needs_retest=statuses.get(TestRunTestCaseStatus.NEEDS_RETEST, 0),
        steps_performed=steps,
        help_resolved=helps,
    )


def _case_filter(filters: LeaderboardFilters, prefix: str = "") -> Q:
    """The project and run-mode filters on a case (``prefix`` reaches the case
    from a related model, e.g. ``"test_run_test_case__"``)."""
    q = Q()
    if filters.project_id is not None:
        q &= Q(**{f"{prefix}test_run__project_id": filters.project_id})
    if filters.help_mode:
        q &= Q(**{f"{prefix}test_run__help_mode": filters.help_mode})
    return q


def _snapshot_case_filter(filters: LeaderboardFilters) -> Q:
    """``_case_filter`` limited to non-archived projects (snapshot metrics)."""
    return _case_filter(filters) & Q(test_run__project__archived=False)


def _counted_cases(
    filters: LeaderboardFilters, start: datetime, end: datetime
) -> QuerySet[TestRunTestCase]:
    """Counted results with ``finished_at`` in ``[start, end)`` (archived
    projects included: windowed history still counts)."""
    return TestRunTestCase.objects.filter(
        _case_filter(filters),
        status__in=COUNTED_STATUSES,
        finished_at__gte=start,
        finished_at__lt=end,
    )


def _reported_steps(
    filters: LeaderboardFilters, start: datetime, end: datetime
) -> QuerySet[CasePersonStep]:
    return CasePersonStep.objects.filter(
        _case_filter(filters, "test_run_test_case__"),
        reported_by__isnull=False,
        reported_at__gte=start,
        reported_at__lt=end,
    )


def _eligible_users(
    filters: LeaderboardFilters, credited_ids: Iterable[int]
) -> list[CustomUser]:
    """Active users with a writable membership of a non-archived project (the
    filtered one if set), plus active ``credited_ids`` (credited with a counted
    result in ``[previous_start, end)`` under the filters, from ``_verdicts``,
    so the cases are scanned once). One query (one subquery)."""
    memberships = ProjectMembership.objects.filter(
        role__in=WRITABLE_ROLES, project__archived=False
    )
    if filters.project_id is not None:
        memberships = memberships.filter(project_id=filters.project_id)
    return list(
        CustomUser.objects.filter(is_active=True)
        .filter(
            Q(pk__in=memberships.values("user_id")) | Q(pk__in=list(credited_ids)),
        )
        .only("id", "email", "first_name", "last_name")
        .order_by()
    )


def _verdicts(
    filters: LeaderboardFilters, window: LeaderboardWindow
) -> tuple[dict[int, tuple[dict[str, int], dict[str, int]]], int]:
    """Per credited user, ``(current, previous)`` counts by status, and the
    current window's results credited to nobody. One query."""
    current = Q(finished_at__gte=window.start, finished_at__lt=window.end)
    previous = Q(
        finished_at__gte=window.previous_start, finished_at__lt=window.previous_end
    )
    annotations = {
        f"{period}_{status}": Count("id", filter=when & Q(status=status))
        for period, when in (("cur", current), ("prev", previous))
        for status in COUNTED_STATUSES
    }
    rows = (
        _counted_cases(filters, window.previous_start, window.end)
        .values("credited_user_id")
        .annotate(**annotations)
        .order_by()
    )
    by_user: dict[int, tuple[dict[str, int], dict[str, int]]] = {}
    unattributed = 0
    for row in rows:
        cur = {s: int(row[f"cur_{s}"]) for s in COUNTED_STATUSES}
        prev = {s: int(row[f"prev_{s}"]) for s in COUNTED_STATUSES}
        user_id: int | None = row["credited_user_id"]
        if user_id is None:
            unattributed = sum(cur.values())
        else:
            by_user[user_id] = (cur, prev)
    return by_user, unattributed


def _windowed_counts(
    rows: Iterable[dict[str, int | None]], user_key: str
) -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    for row in rows:
        user_id = row[user_key]
        if user_id is not None:
            result[user_id] = (int(row["cur"] or 0), int(row["prev"] or 0))
    return result


def _person_steps(
    filters: LeaderboardFilters, window: LeaderboardWindow
) -> dict[int, tuple[int, int]]:
    """Per reporter, ``(current, previous)`` reported person steps. One query."""
    rows = (
        _reported_steps(filters, window.previous_start, window.end)
        .values("reported_by_id")
        .annotate(
            cur=Count(
                "id",
                filter=Q(reported_at__gte=window.start, reported_at__lt=window.end),
            ),
            prev=Count(
                "id",
                filter=Q(
                    reported_at__gte=window.previous_start,
                    reported_at__lt=window.previous_end,
                ),
            ),
        )
        .order_by()
    )
    return _windowed_counts(rows, "reported_by_id")


def _help_resolved(
    filters: LeaderboardFilters, window: LeaderboardWindow
) -> dict[int, tuple[int, int]]:
    """Per resolver, ``(current, previous)`` help requests a person resolved
    (Continue, Mark failed, Mark blocked). One query."""
    rows = (
        CaseHelpRequest.objects.filter(
            _case_filter(filters, "test_run_test_case__"),
            resolved_by__isnull=False,
            resolution__in=CREDITED_HELP_RESOLUTIONS,
            resolved_at__gte=window.previous_start,
            resolved_at__lt=window.end,
        )
        .values("resolved_by_id")
        .annotate(
            cur=Count(
                "id",
                filter=Q(resolved_at__gte=window.start, resolved_at__lt=window.end),
            ),
            prev=Count(
                "id",
                filter=Q(
                    resolved_at__gte=window.previous_start,
                    resolved_at__lt=window.previous_end,
                ),
            ),
        )
        .order_by()
    )
    return _windowed_counts(rows, "resolved_by_id")


def _active_dates(
    filters: LeaderboardFilters, window: LeaderboardWindow
) -> dict[int, set[date]]:
    """Per user, the local days of the current window with a counted verdict
    credited to them or a person step they reported. One ``UNION`` query."""
    tz = timezone.get_current_timezone()
    verdict_days = (
        _counted_cases(filters, window.start, window.end)
        .filter(credited_user__isnull=False)
        .annotate(user=F("credited_user_id"), day=TruncDate("finished_at", tzinfo=tz))
        .values_list("user", "day")
        .order_by()
        .distinct()
    )
    step_days = (
        _reported_steps(filters, window.start, window.end)
        .annotate(user=F("reported_by_id"), day=TruncDate("reported_at", tzinfo=tz))
        .values_list("user", "day")
        .order_by()
        .distinct()
    )
    result: dict[int, set[date]] = {}
    for user_id, day in verdict_days.union(step_days):
        result.setdefault(user_id, set()).add(day)
    return result


def _need_help_now(filters: LeaderboardFilters) -> dict[int, int]:
    """Per credited user, cases waiting on a person right now (Need help or
    Your turn) in non-archived projects. One query."""
    rows = (
        TestRunTestCase.objects.filter(
            _snapshot_case_filter(filters),
            status__in=WAITING_ON_PERSON,
            credited_user__isnull=False,
        )
        .values("credited_user_id")
        .annotate(n=Count("id"))
        .order_by()
    )
    return {row["credited_user_id"]: int(row["n"]) for row in rows}


def _remaining(filters: LeaderboardFilters) -> dict[int, int]:
    """Per user, cases with no verdict yet in open runs (Draft, Running, Needs
    help) of the non-archived projects they may write to. Two queries; the
    sum over memberships is done here."""
    memberships = ProjectMembership.objects.filter(
        role__in=WRITABLE_ROLES, project__archived=False
    )
    if filters.project_id is not None:
        memberships = memberships.filter(project_id=filters.project_id)
    member_projects = list(memberships.values_list("user_id", "project_id"))

    per_project: dict[int, int] = {
        row["test_run__project_id"]: int(row["n"])
        for row in TestRunTestCase.objects.filter(
            _snapshot_case_filter(filters),
            test_run__status__in=OPEN_RUN_STATUSES,
        )
        .exclude(status__in=FINISHED_CASE_STATUSES)
        .values("test_run__project_id")
        .annotate(n=Count("id"))
        .order_by()
    }
    result: dict[int, int] = {}
    for user_id, project_id in member_projects:
        count = per_project.get(project_id, 0)
        if count:
            result[user_id] = result.get(user_id, 0) + count
    return result
