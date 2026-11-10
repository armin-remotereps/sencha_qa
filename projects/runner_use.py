"""Whether a Test Runner is held, and by what: the one definition (a leaf).

A runner serves at most one holder at a time:

- an active agent run (STARTED or PAUSED, not Manual) assigned to it;
- a claimed, unpaused IN_PROGRESS case of a STARTED Manual run assigned to it
  (a claim at the runner window or through MCP);
- a live Machine setup turn that recorded it;
- an in-flight self-update of it;
- a readiness scan or quick check locked on it.

Every acquiring service takes the runner row lock (``select_for_update``) and
then calls ``runner_hold``; the lock order is runner, then project (while the
Phase 1 per-project rule lives), then run. It imports only models and other
leaves (``machine_setup.turn_state``, ``machine_readiness.project_locks``,
``projects.runner_update_state``), so every app may use it. It never imports
``projects.services``.

Replaces the former per-project ``run_machine_use`` module.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.core.cache import cache
from django.db.models import Q, QuerySet
from django.utils import timezone

from machine_readiness.project_locks import (
    RUNNER_QUICK_CHECK_LOCK_PREFIX,
    RUNNER_SCAN_LOCK_PREFIX,
    is_locked,
    lock_key,
)
from machine_setup.turn_state import live_turn_runner_ids, runner_turn_live
from projects.models import (
    ACTIVE_RUN_STATUSES,
    NON_TERMINAL_RUNNER_UPDATE_STATUSES,
    RunHelpMode,
    RunnerUpdate,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.runner_update_state import runner_update_timeout
from runners.types import RunnerId


class RunnerHoldKind(StrEnum):
    AGENT_RUN = "agent_run"
    MANUAL_CASE = "manual_case"
    SETUP_TURN = "setup_turn"
    RUNNER_UPDATE = "runner_update"
    READINESS_SCAN = "readiness_scan"
    READINESS_CHECK = "readiness_check"


_HOLD_MESSAGES: dict[RunnerHoldKind, str] = {
    RunnerHoldKind.AGENT_RUN: "The Test Runner is busy with another run.",
    RunnerHoldKind.MANUAL_CASE: (
        "A tester is performing a Manual case on the Test Runner."
    ),
    RunnerHoldKind.SETUP_TURN: "Machine setup is running on the Test Runner.",
    RunnerHoldKind.RUNNER_UPDATE: "The Test Runner is being updated.",
    RunnerHoldKind.READINESS_SCAN: (
        "The Test Runner is scanning the artifacts folder."
    ),
    RunnerHoldKind.READINESS_CHECK: "The Test Runner is checking the machine.",
}
# Every hold text, for surfaces that must word them neutrally (MCP).
RUNNER_HOLD_MESSAGES: frozenset[str] = frozenset(_HOLD_MESSAGES.values())


@dataclass(frozen=True)
class RunnerHold:
    """Why a runner is held. ``message`` is neutral (no project or case names),
    safe for any viewer; ``project_id`` / ``test_run_id`` let a page link the
    holder when the viewer may see that project."""

    kind: RunnerHoldKind
    message: str
    project_id: int | None = None
    test_run_id: int | None = None


# A hold the caller accepts (``runner_hold(allow=)``): skipped, and the
# later holders are still checked.
HoldFilter = Callable[[RunnerHold], bool]


def _hold(
    kind: RunnerHoldKind,
    project_id: int | None = None,
    test_run_id: int | None = None,
) -> RunnerHold:
    return RunnerHold(
        kind=kind,
        message=_HOLD_MESSAGES[kind],
        project_id=project_id,
        test_run_id=test_run_id,
    )


def runner_hold(
    runner_id: RunnerId,
    *,
    now: datetime | None = None,
    ignore_run_id: int | None = None,
    ignore_turn_id: int | None = None,
    allow: HoldFilter | None = None,
) -> RunnerHold | None:
    """The runner's first holder that counts, or None when it is free.

    ``ignore_run_id``: an active agent run that already holds the runner and
    is the one asking (Resume, help Continue, a reassign to the same runner)
    does not count against itself. Manual claims are never ignored by it:
    one runner serves one claimed case. ``ignore_turn_id``: likewise for a
    setup turn re-checking its own hold (or a live auto-fix turn a wake path
    is about to stop). ``allow``: holds the caller accepts (a paused run or a
    claimed case of its own project, its own readiness lock); an allowed
    hold is skipped and the later holders are still checked.

    Holders in order: agent runs, claimed Manual cases, a setup turn, a
    self-update, a readiness scan, a readiness check. At most five cheap
    queries plus two cache reads, stopping at the first hold that counts.
    Call it under the runner row lock (``runners.locks``) when acquiring.
    """
    for hold in _iter_holds(
        runner_id,
        now or timezone.now(),
        ignore_run_id=ignore_run_id,
        ignore_turn_id=ignore_turn_id,
    ):
        if allow is None or not allow(hold):
            return hold
    return None


def _iter_holds(
    runner_id: RunnerId,
    now: datetime,
    *,
    ignore_run_id: int | None,
    ignore_turn_id: int | None,
) -> Iterator[RunnerHold]:
    """Every holder of the runner, lazily, in ``runner_hold``'s order."""
    agent_runs = _agent_runs(Q(runner_id=runner_id))
    if ignore_run_id is not None:
        agent_runs = agent_runs.exclude(pk=ignore_run_id)
    for run_id, project_id in agent_runs.order_by("pk").values_list("pk", "project_id"):
        yield _hold(RunnerHoldKind.AGENT_RUN, project_id, run_id)

    for run_id, project_id in (
        _claimed_unpaused_manual_cases(Q(test_run__runner_id=runner_id))
        .order_by("pk")
        .values_list("test_run_id", "test_run__project_id")
    ):
        yield _hold(RunnerHoldKind.MANUAL_CASE, project_id, run_id)

    if runner_turn_live(runner_id, now, exclude_turn_id=ignore_turn_id):
        yield _hold(RunnerHoldKind.SETUP_TURN)
    if runner_update_in_flight(runner_id, now):
        yield _hold(RunnerHoldKind.RUNNER_UPDATE)
    if is_locked(RUNNER_SCAN_LOCK_PREFIX, runner_id):
        yield _hold(RunnerHoldKind.READINESS_SCAN)
    if is_locked(RUNNER_QUICK_CHECK_LOCK_PREFIX, runner_id):
        yield _hold(RunnerHoldKind.READINESS_CHECK)


def runner_holds(
    runner_ids: Collection[RunnerId], *, now: datetime | None = None
) -> dict[RunnerId, RunnerHold]:
    """The first holder of each held runner among ``runner_ids`` (free ones
    are missing), with ``runner_hold``'s order and no ignores.

    Bulk form for lists and defaults: four queries and one cache read
    whatever the number of runners (none for an empty input).
    """
    ids = sorted({int(runner_id) for runner_id in runner_ids})
    if not ids:
        return {}
    now = now or timezone.now()
    holds: dict[RunnerId, RunnerHold] = {}

    def put(raw_id: int | None, hold: RunnerHold) -> None:
        if raw_id is not None:
            holds.setdefault(RunnerId(raw_id), hold)

    for run_id, project_id, raw_id in (
        _agent_runs(Q(runner_id__in=ids))
        .order_by("pk")
        .values_list("pk", "project_id", "runner_id")
    ):
        put(raw_id, _hold(RunnerHoldKind.AGENT_RUN, project_id, run_id))
    for run_id, project_id, raw_id in (
        _claimed_unpaused_manual_cases(Q(test_run__runner_id__in=ids))
        .order_by("pk")
        .values_list("test_run_id", "test_run__project_id", "test_run__runner_id")
    ):
        put(raw_id, _hold(RunnerHoldKind.MANUAL_CASE, project_id, run_id))
    for raw_id in live_turn_runner_ids(ids, now):
        put(raw_id, _hold(RunnerHoldKind.SETUP_TURN))
    for raw_id in RunnerUpdate.objects.filter(
        runner_id__in=ids,
        status__in=NON_TERMINAL_RUNNER_UPDATE_STATUSES,
        created_at__gte=now - runner_update_timeout(),
    ).values_list("runner_id", flat=True):
        put(raw_id, _hold(RunnerHoldKind.RUNNER_UPDATE))
    lock_keys = {
        lock_key(prefix, runner_id): (runner_id, kind)
        for runner_id in ids
        for prefix, kind in (
            (RUNNER_SCAN_LOCK_PREFIX, RunnerHoldKind.READINESS_SCAN),
            (RUNNER_QUICK_CHECK_LOCK_PREFIX, RunnerHoldKind.READINESS_CHECK),
        )
    }
    found = cache.get_many(list(lock_keys))
    # Scan before check, as ``runner_hold`` orders them.
    for key, (runner_id, kind) in lock_keys.items():
        if found.get(key) is not None:
            put(runner_id, _hold(kind))
    return holds


def agent_run_started_on_runner(runner_id: RunnerId) -> bool:
    """A STARTED, non-Manual run is executing on the runner (one query).

    Per runner (formerly per project): Machine setup
    sends and readiness gates refuse while it holds.
    """
    return (
        TestRun.objects.filter(runner_id=runner_id, status=TestRunStatus.STARTED)
        .exclude(help_mode=RunHelpMode.MANUAL)
        .exists()
    )


def manual_tester_on_runner(runner_id: RunnerId) -> bool:
    """A claimed, unpaused Manual case uses the runner (one query).

    Per runner (formerly per project).
    """
    return _claimed_unpaused_manual_cases(Q(test_run__runner_id=runner_id)).exists()


def runner_update_in_flight(runner_id: RunnerId, now: datetime) -> bool:
    """An in-flight self-update of the runner younger than the timeout."""
    return RunnerUpdate.objects.filter(
        runner_id=runner_id,
        status__in=NON_TERMINAL_RUNNER_UPDATE_STATUSES,
        created_at__gte=now - runner_update_timeout(),
    ).exists()


def _agent_runs(runner_q: Q) -> QuerySet[TestRun]:
    """Active (STARTED or PAUSED) non-Manual runs matching ``runner_q``."""
    return TestRun.objects.filter(runner_q, status__in=ACTIVE_RUN_STATUSES).exclude(
        help_mode=RunHelpMode.MANUAL
    )


def _claimed_unpaused_manual_cases(runner_q: Q) -> QuerySet[TestRunTestCase]:
    """Same rule as ``manual_run_policy.is_claimed_manual_case`` plus not
    paused, on runs matching ``runner_q``; keep both aligned."""
    return TestRunTestCase.objects.filter(
        runner_q,
        test_run__status=TestRunStatus.STARTED,
        test_run__help_mode=RunHelpMode.MANUAL,
        status=TestRunTestCaseStatus.IN_PROGRESS,
        manual_paused_at__isnull=True,
    ).exclude(manual_claim_token="")


def runner_id_of_run(test_run: TestRun) -> RunnerId | None:
    """The run's assigned runner as a typed id (no query), or None."""
    return None if test_run.runner_id is None else RunnerId(test_run.runner_id)
