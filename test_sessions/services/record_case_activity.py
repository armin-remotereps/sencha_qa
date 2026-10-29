"""Attribute run cases to the acting user's open test session.

Called by ``projects`` at each action site, inside the action's transaction
and after its locks. Bookkeeping never fails a test action: every entry point
runs in a savepoint, catches ``DatabaseError`` only, logs it and returns.

Set-based throughout: the query count does not depend on the number of cases.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import F, OuterRef, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import CustomUser
from projects.models import FINISHED_CASE_STATUSES, TestRunTestCase
from test_sessions.case_activity import VERDICT_DISCARDING, CaseActivity
from test_sessions.models import TestSession, TestSessionCase

logger = logging.getLogger(__name__)


def _pivot_value(field: str) -> Subquery:
    """The row's live pivot ``field`` (a correlated subquery)."""
    return Subquery(
        TestRunTestCase.objects.filter(pk=OuterRef("test_run_test_case_id")).values(
            field
        )[:1]
    )


def _freeze(rows: QuerySet[TestSessionCase], now: datetime) -> int:
    """Copy the live pivot outcome of unfrozen ``rows`` into ``frozen_*``.

    One UPDATE. Rows without a pivot are skipped: there is nothing to copy.
    """
    return rows.filter(frozen_at__isnull=True, test_run_test_case__isnull=False).update(
        frozen_at=now,
        frozen_status=Coalesce(_pivot_value("status"), Value("")),
        frozen_failed_by=Coalesce(_pivot_value("failed_by"), Value("")),
        frozen_blocked_reason=Coalesce(_pivot_value("blocked_reason"), Value("")),
        frozen_started_at=_pivot_value("started_at"),
        frozen_finished_at=_pivot_value("finished_at"),
        frozen_active_duration=_pivot_value("active_duration"),
        frozen_result=Coalesce(_pivot_value("result"), Value("")),
    )


def _active_session_id(user: CustomUser, now: datetime) -> int | None:
    """The user's open session, unless it is past ``TEST_SESSION_MAX_HOURS``
    and only waits for the auto-close beat (it ends at ``started_at + max``,
    so later work does not belong to it)."""
    sessions = TestSession.objects.filter(user=user, ended_at__isnull=True)
    max_hours: int = settings.TEST_SESSION_MAX_HOURS
    if max_hours > 0:
        sessions = sessions.filter(started_at__gt=now - timedelta(hours=max_hours))
    return sessions.values_list("id", flat=True).first()


def _record(
    user: CustomUser | None, pivot_ids: list[int], activity: CaseActivity
) -> None:
    now = timezone.now()
    # The session first: most actions are by users without an open session,
    # who then pay for at most this query and the freeze below.
    session_id = _active_session_id(user, now) if user is not None else None

    if activity in VERDICT_DISCARDING:
        others = TestSessionCase.objects.filter(test_run_test_case_id__in=pivot_ids)
        if session_id is not None:
            others = others.exclude(session_id=session_id)
        _freeze(others, now)

    if session_id is None:
        return

    pivots = list(
        TestRunTestCase.objects.filter(pk__in=pivot_ids).values(
            "id",
            "test_run_id",
            "test_run__name",
            "test_run__help_mode",
            "test_run__project_id",
            "test_run__project__name",
            "test_case__title",
            "test_case__testrail_id",
        )
    )
    if not pivots:
        return
    # Insert in the caller's order (the run's case order), so the session's
    # rows and its export follow it.
    position = {pivot_id: index for index, pivot_id in enumerate(pivot_ids)}
    pivots.sort(key=lambda pivot: position[pivot["id"]])
    found_ids = [pivot["id"] for pivot in pivots]

    is_person_step = activity == CaseActivity.PERSON_STEP
    # Existing rows first, so the insert below only adds the missing ones.
    TestSessionCase.objects.filter(
        session_id=session_id, test_run_test_case_id__in=found_ids
    ).update(
        last_activity=activity,
        last_activity_at=now,
        activity_count=F("activity_count") + 1,
        person_step_count=F("person_step_count") + (1 if is_person_step else 0),
        run_help_mode=Coalesce(_pivot_value("test_run__help_mode"), Value("")),
        run_name=Coalesce(_pivot_value("test_run__name"), Value("")),
        project_name=Coalesce(_pivot_value("test_run__project__name"), Value("")),
        case_title=Coalesce(_pivot_value("test_case__title"), Value("")),
        testrail_case_id=Coalesce(_pivot_value("test_case__testrail_id"), Value("")),
        frozen_at=None,
        frozen_status="",
        frozen_failed_by="",
        frozen_blocked_reason="",
        frozen_started_at=None,
        frozen_finished_at=None,
        frozen_active_duration=None,
        frozen_result="",
    )
    # Rows that already existed hit the unique constraint and are skipped.
    TestSessionCase.objects.bulk_create(
        [
            TestSessionCase(
                session_id=session_id,
                test_run_test_case_id=pivot["id"],
                project_id=pivot["test_run__project_id"],
                test_run_id=pivot["test_run_id"],
                project_name=pivot["test_run__project__name"],
                run_name=pivot["test_run__name"],
                run_ref=pivot["test_run_id"],
                pivot_ref=pivot["id"],
                case_title=pivot["test_case__title"],
                testrail_case_id=pivot["test_case__testrail_id"],
                first_activity=activity,
                first_activity_at=now,
                last_activity=activity,
                last_activity_at=now,
                activity_count=1,
                person_step_count=1 if is_person_step else 0,
                run_help_mode=pivot["test_run__help_mode"],
            )
            for pivot in pivots
        ],
        ignore_conflicts=True,
    )


def record_case_activity(
    *, user: CustomUser | None, pivot_ids: Sequence[int], activity: CaseActivity
) -> None:
    """Record ``activity`` on ``pivot_ids`` in ``user``'s open session.

    For verdict-discarding activities, first freeze the other sessions' rows
    of those cases. Without a user or an open session nothing is recorded.
    At most 5 queries, whatever the number of cases; 1 or 2 without an
    open session.
    """
    if not pivot_ids:
        return
    ids = list(pivot_ids)
    try:
        with transaction.atomic():
            _record(user, ids, activity)
    except DatabaseError:
        logger.exception(
            "Could not record test session activity %s for run cases %s",
            activity,
            ids,
        )


def freeze_session_cases_of_run(test_run_id: int) -> None:
    """Freeze every unfrozen session row of ``test_run_id``'s cases (Reset)."""
    try:
        with transaction.atomic():
            _freeze(
                TestSessionCase.objects.filter(test_run_id=test_run_id),
                timezone.now(),
            )
    except DatabaseError:
        logger.exception(
            "Could not freeze test session cases of test run %s", test_run_id
        )


def freeze_finished_cases_of_sessions(
    session_ids: Sequence[int], now: datetime
) -> None:
    """Freeze the sessions' unfrozen rows whose case has finished (Stop and
    auto-close), in one UPDATE.

    It can deadlock with a concurrent Resume/Re-run freezing the same rows;
    Postgres then aborts this savepoint and those rows stay live (logged).
    Accepted: they still follow the same execution."""
    if not session_ids:
        return
    try:
        with transaction.atomic():
            _freeze(
                TestSessionCase.objects.filter(
                    session_id__in=list(session_ids),
                    test_run_test_case__status__in=FINISHED_CASE_STATUSES,
                ),
                now,
            )
    except DatabaseError:
        logger.exception(
            "Could not freeze finished cases of test sessions %s", list(session_ids)
        )
