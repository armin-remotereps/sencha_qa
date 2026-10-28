"""How long a test run took."""

from __future__ import annotations

from datetime import timedelta

from projects.models import TestRun


def run_duration(test_run: TestRun) -> timedelta | None:
    """Wall clock from the run's first Start to its latest finish.

    Includes time spent in Needs help and, after a Resume or Re-run, the idle
    gap since the original Start (``started_at`` is kept by both). None while
    either end is missing.
    """
    if test_run.started_at is None or test_run.finished_at is None:
        return None
    return test_run.finished_at - test_run.started_at
