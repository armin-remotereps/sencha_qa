"""Time limits of a setup turn and the "is a turn live" test built on them.

A leaf module (imports only ``machine_setup.models``, ``runners.types`` and settings) so ``projects`` can gate
run start and resume on it without importing the agent runtime.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from machine_setup.models import LIVE_TURN_STATUSES, SetupTurn
from runners.types import RunnerId

# Celery limits of `run_setup_turn`: the agent deadline plus headroom for an
# in-flight tool. A live turn older than the hard limit plus a minute belongs
# to a killed worker.
SETUP_TURN_SOFT_TIME_LIMIT: int = settings.MACHINE_SETUP_TURN_TIMEOUT_SECONDS + 300
SETUP_TURN_TIME_LIMIT: int = SETUP_TURN_SOFT_TIME_LIMIT + 60
SETUP_TURN_STALE_AFTER = timedelta(seconds=SETUP_TURN_TIME_LIMIT + 60)


# Why run start, resume and the readiness check refuse while a turn is live.
MACHINE_SETUP_RUNNING_MESSAGE = (
    "Machine setup is running for this project. Stop it or wait for it to finish."
)


def is_turn_stale(turn: SetupTurn, now: datetime) -> bool:
    """Whether a queued or running turn has outlived any worker that could run it."""
    if turn.status not in LIVE_TURN_STATUSES:
        return False
    since = turn.started_at or turn.created_at
    return since < now - SETUP_TURN_STALE_AFTER


def _not_stale(now: datetime) -> Q:
    """Turns started (or, still queued, created) within the stale window."""
    cutoff = now - SETUP_TURN_STALE_AFTER
    return Q(started_at__gte=cutoff) | Q(
        started_at__isnull=True, created_at__gte=cutoff
    )


def live_turn_exists(project_id: int, now: datetime) -> bool:
    """Whether a queued or running, non-stale turn exists for the project."""
    return SetupTurn.objects.filter(
        _not_stale(now),
        project_id=project_id,
        status__in=LIVE_TURN_STATUSES,
    ).exists()


def runner_turn_live(
    runner_id: RunnerId, now: datetime, *, exclude_turn_id: int | None = None
) -> bool:
    """Whether a queued or running, non-stale turn drives the runner.

    ``exclude_turn_id`` leaves out the asking turn itself (a turn re-checking
    its own hold under the runner lock).
    """
    turns = SetupTurn.objects.filter(
        _not_stale(now),
        runner_id=runner_id,
        status__in=LIVE_TURN_STATUSES,
    )
    if exclude_turn_id is not None:
        turns = turns.exclude(pk=exclude_turn_id)
    return turns.exists()


def live_turn_runner_ids(runner_ids: Collection[int], now: datetime) -> set[int]:
    """The runners among ``runner_ids`` that a queued or running, non-stale
    turn drives (one query; bulk form of ``runner_turn_live``)."""
    if not runner_ids:
        return set()
    return {
        runner_id
        for runner_id in SetupTurn.objects.filter(
            _not_stale(now),
            runner_id__in=list(runner_ids),
            status__in=LIVE_TURN_STATUSES,
        ).values_list("runner_id", flat=True)
        if runner_id is not None
    }


def auto_fix_turn_live(help_request_id: int) -> bool:
    """Whether an auto-fix turn of the help request is queued or running (not stale)."""
    return live_auto_fix_turn_id(help_request_id) is not None


def live_auto_fix_turn_id(help_request_id: int) -> int | None:
    """The queued or running (not stale) auto-fix turn of the help request,
    or None (one query). Wake paths pass it as the turn to ignore on the
    run's runner: resolving the request stops it."""
    return (
        SetupTurn.objects.filter(
            _not_stale(timezone.now()),
            auto_fix=True,
            conversation__help_request_id=help_request_id,
            status__in=LIVE_TURN_STATUSES,
        )
        .values_list("pk", flat=True)
        .first()
    )


def run_auto_fix_turn_live(test_run_id: int) -> bool:
    """Whether an auto-fix turn of any help request of the run is queued or
    running (not stale); one query."""
    return SetupTurn.objects.filter(
        _not_stale(timezone.now()),
        auto_fix=True,
        conversation__help_request__test_run_test_case__test_run_id=test_run_id,
        status__in=LIVE_TURN_STATUSES,
    ).exists()


def person_turn_live(help_request_id: int) -> bool:
    """Whether a person's (help chat) turn of the help request is live (not stale)."""
    return _help_turn_live(help_request_id, auto_fix=False)


def _help_turn_live(help_request_id: int, *, auto_fix: bool) -> bool:
    return SetupTurn.objects.filter(
        _not_stale(timezone.now()),
        auto_fix=auto_fix,
        conversation__help_request_id=help_request_id,
        status__in=LIVE_TURN_STATUSES,
    ).exists()


def manual_case_turn_live(pivot_id: int) -> bool:
    """Whether a turn of the run case's manual discussion is live (not stale).

    Continue of a paused manual case is refused meanwhile: the agent may be
    using the machine.
    """
    return SetupTurn.objects.filter(
        _not_stale(timezone.now()),
        conversation__manual_case_id=pivot_id,
        status__in=LIVE_TURN_STATUSES,
    ).exists()


def live_turn_manual_case_ids(project_id: int, now: datetime) -> set[int | None]:
    """The ``manual_case`` of the thread of each queued or running, non-stale
    turn of the project (None for a thread of another kind); one query.

    Empty exactly when ``live_turn_exists`` is false, so one read answers
    both "is some turn live" and "is this case's discussion turn live".
    """
    return set(
        SetupTurn.objects.filter(
            _not_stale(now),
            project_id=project_id,
            status__in=LIVE_TURN_STATUSES,
        ).values_list("conversation__manual_case_id", flat=True)
    )
