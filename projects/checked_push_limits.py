"""Time limits of "Send checked to TestRail" and the live-send test built on them.

Kept apart from `testrail_checked_push_services` (which imports
`projects.services`) so `projects.services.reset_test_run` can use them without
an import cycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from projects.models import TestRailPushStatus, TestRun

# Celery limits of `push_checked_results_to_testrail`. A PUSHING status older
# than the hard limit plus a minute belongs to a killed worker.
CHECKED_PUSH_SOFT_TIME_LIMIT = 3600
CHECKED_PUSH_TIME_LIMIT = CHECKED_PUSH_SOFT_TIME_LIMIT + 60
CHECKED_PUSH_STALE_AFTER = timedelta(seconds=CHECKED_PUSH_TIME_LIMIT + 60)


def is_checked_push_live(test_run: TestRun, now: datetime) -> bool:
    """Whether a send holds the run's PUSHING slot and is not stale."""
    started_at = test_run.testrail_source_push_started_at
    return (
        test_run.testrail_source_push_status == TestRailPushStatus.PUSHING
        and started_at is not None
        and started_at >= now - CHECKED_PUSH_STALE_AFTER
    )


def is_checked_push_stale(test_run: TestRun, now: datetime) -> bool:
    """Whether the run is PUSHING but its send was interrupted (worker gone)."""
    return (
        test_run.testrail_source_push_status == TestRailPushStatus.PUSHING
        and not is_checked_push_live(test_run, now)
    )
