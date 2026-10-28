"""Track and report how long a run case actually ran (its active execution time).

A case runs in one or more *segments*: each starts when a worker picks the case
up and ends when that worker stops owning it (verdict, pause for help, cancel).
``TestRunTestCase.active_duration`` is the sum of the closed segments since the
case last started from scratch (Start or Re-run, which clear it); time spent
waiting for a person, in a Night auto-fix or Blocked before a Resume is not
counted. Null means unknown (a row older than the field, never backfilled).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Avg, Count, DateTimeField, F, Q, Value
from django.db.models.functions import Greatest

from projects.models import TestRun, TestRunTestCase, TestRunTestCaseStatus

logger = logging.getLogger(__name__)

# Fields open_case_segment may change; save them with the status.
CASE_SEGMENT_OPEN_FIELDS: tuple[str, ...] = (
    "segment_started_at",
    "started_at",
    "active_duration",
)

# Statuses whose durations make up a run's average time per case.
AVERAGE_CASE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
)


@dataclass(frozen=True)
class RunCaseDurationStats:
    # Mean active duration of the timed Passed / Failed cases; None if none.
    average: timedelta | None
    timed_count: int
    # Passed / Failed cases with an unknown duration (left out of the average).
    untimed_count: int


def open_case_segment(pivot: TestRunTestCase, now: datetime) -> None:
    """Start a segment on the in-memory ``pivot``; the caller saves it.

    Save ``CASE_SEGMENT_OPEN_FIELDS`` in the same ``save(update_fields=...)``
    as the In progress status. A fresh execution (``started_at`` null) also
    sets ``started_at`` and starts ``active_duration`` at zero; a continued one
    keeps both, so a row with an unknown duration stays unknown until it is
    re-run. A segment still open (redelivered task, killed worker) is lost.
    """
    if pivot.segment_started_at is not None:
        logger.warning(
            "Run case %s opened a segment while one was open; that one is lost",
            pivot.pk,
        )
    if pivot.started_at is None:
        pivot.started_at = now
        pivot.active_duration = timedelta(0)
    pivot.segment_started_at = now


def close_case_segment(pivot_id: int, now: datetime) -> None:
    """End the case's open segment, adding its length to ``active_duration``.

    One conditional UPDATE on the row, so it is idempotent (a second close adds
    nothing) and independent of any stale in-memory pivot. An unknown
    (null) ``active_duration`` stays null; a negative span (clock skew) adds
    zero.
    """
    segment = Greatest(
        Value(now, output_field=DateTimeField()) - F("segment_started_at"),
        Value(timedelta(0)),
    )
    TestRunTestCase.objects.filter(
        pk=pivot_id, segment_started_at__isnull=False
    ).update(
        active_duration=F("active_duration") + segment,
        segment_started_at=None,
    )


def get_run_case_duration_stats(test_run: TestRun) -> RunCaseDurationStats:
    """Average time per Passed / Failed case of the run (one aggregate)."""
    timed = Q(active_duration__isnull=False)
    row = test_run.pivot_entries.filter(status__in=AVERAGE_CASE_STATUSES).aggregate(
        average=Avg("active_duration"),
        timed_count=Count("id", filter=timed),
        untimed_count=Count("id", filter=~timed),
    )
    average = row["average"]
    return RunCaseDurationStats(
        average=average if isinstance(average, timedelta) else None,
        timed_count=int(row["timed_count"]),
        untimed_count=int(row["untimed_count"]),
    )
