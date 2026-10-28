"""Machine health insights: why cases needed help, how Night auto-fix did and
how help requests were resolved, over a recent window of days.

Scoped to the user's non-archived member projects (optionally one of them),
which the caller passes in. Bounded: three aggregate queries whatever the
window or the number of help requests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Count, QuerySet
from django.db.models.functions import TruncDate
from django.utils import timezone

from projects.models import (
    BlockedReason,
    CaseHelpRequest,
    HelpAutoFixState,
    HelpResolution,
)

MACHINE_HEALTH_DAY_CHOICES: tuple[int, ...] = (7, 30, 90)
DEFAULT_MACHINE_HEALTH_DAYS = 30

AGENT_ASKED_LABEL = "Agent asked"

# Bar widths are rendered with the ``.bar-w-0`` … ``.bar-w-100`` classes.
BAR_STEP_PERCENT = 5


@dataclass(frozen=True)
class FaultCount:
    """One fault (or "Agent asked" for a blank fault) and its request count."""

    fault: str
    label: str
    count: int
    # Share of the largest count in its list, for the bar width: 0-100 in
    # steps of BAR_STEP_PERCENT (a ``.bar-w-N`` class).
    bar_percent: int


@dataclass(frozen=True)
class HealthDay:
    """One day of the window: total help requests and the faults behind them."""

    day: date
    total: int
    bar_percent: int
    faults: list[FaultCount]


@dataclass(frozen=True)
class AutoFixStats:
    """Night auto-fix outcomes of the window's help requests."""

    fixed: int
    not_fixed: int
    running: int
    # ``fixed / (fixed + not_fixed)`` as a whole percent; None when none ended.
    success_percent: int | None

    @property
    def ended(self) -> int:
        return self.fixed + self.not_fixed


@dataclass(frozen=True)
class ResolutionCount:
    """How many of the window's help requests were closed one way (or open)."""

    resolution: str
    label: str
    count: int
    bar_percent: int


@dataclass(frozen=True)
class MachineHealth:
    days: int
    start_day: date
    end_day: date
    total_requests: int
    # Oldest first, one entry per day of the window (days without requests too).
    per_day: list[HealthDay]
    # Most frequent first; only faults that occurred.
    by_fault: list[FaultCount]
    auto_fix: AutoFixStats
    # Most frequent first; only resolutions that occurred.
    resolutions: list[ResolutionCount]


def _percent(part: int, whole: int) -> int:
    return round(part * 100 / whole) if whole else 0


def _bar_percent(part: int, whole: int) -> int:
    """``part / whole`` as a bar width, 0-100 in steps of
    ``BAR_STEP_PERCENT`` (the ``.bar-w-N`` classes); a non-zero part never
    rounds down to an empty bar."""
    if not whole or not part:
        return 0
    step = round(part * 100 / whole / BAR_STEP_PERCENT) * BAR_STEP_PERCENT
    return max(BAR_STEP_PERCENT, min(100, step))


def _fault_label(fault: str) -> str:
    if not fault:
        return AGENT_ASKED_LABEL
    try:
        return str(BlockedReason(fault).label)
    except ValueError:
        return fault


def _resolution_label(resolution: str) -> str:
    try:
        return str(HelpResolution(resolution).label)
    except ValueError:
        return resolution


def _fault_counts(counts: dict[str, int]) -> list[FaultCount]:
    ordered = sorted(counts.items(), key=lambda item: (-item[1], _fault_label(item[0])))
    top = ordered[0][1] if ordered else 0
    return [
        FaultCount(
            fault=fault,
            label=_fault_label(fault),
            count=count,
            bar_percent=_bar_percent(count, top),
        )
        for fault, count in ordered
    ]


def _window_requests(
    project_ids: list[int], start: datetime
) -> QuerySet[CaseHelpRequest]:
    return CaseHelpRequest.objects.filter(
        requested_at__gte=start,
        test_run_test_case__test_run__project_id__in=project_ids,
    )


def get_machine_health(
    member_project_ids: Sequence[int], project_id: int | None, days: int
) -> MachineHealth:
    """Help-request health of the user's projects (``member_project_ids``,
    their member non-archived projects) over the last ``days`` days (today
    included). A ``project_id`` outside them yields an empty result rather
    than another project's data."""
    if days not in MACHINE_HEALTH_DAY_CHOICES:
        days = DEFAULT_MACHINE_HEALTH_DAYS
    end_day = timezone.localdate()
    start_day = end_day - timedelta(days=days - 1)
    start = timezone.make_aware(datetime.combine(start_day, time.min))

    project_ids = list(member_project_ids)
    if project_id is not None:
        project_ids = [pid for pid in project_ids if pid == project_id]

    day_fault: dict[date, dict[str, int]] = {}
    fault_totals: dict[str, int] = {}
    auto_fix_counts: dict[str, int] = {}
    resolution_counts: dict[str, int] = {}
    if project_ids:
        requests = _window_requests(project_ids, start)
        for row in (
            requests.annotate(day=TruncDate("requested_at"))
            .values("day", "fault")
            .annotate(count=Count("id"))
            .order_by()
        ):
            day_fault.setdefault(row["day"], {})[row["fault"]] = row["count"]
            fault_totals[row["fault"]] = (
                fault_totals.get(row["fault"], 0) + row["count"]
            )
        auto_fix_counts = {
            row["auto_fix_state"]: row["count"]
            for row in requests.filter(
                auto_fix_state__in=[
                    HelpAutoFixState.FIXED,
                    HelpAutoFixState.NOT_FIXED,
                    HelpAutoFixState.RUNNING,
                ]
            )
            .values("auto_fix_state")
            .annotate(count=Count("id"))
            .order_by()
        }
        resolution_counts = {
            row["resolution"]: row["count"]
            for row in requests.values("resolution")
            .annotate(count=Count("id"))
            .order_by()
        }

    day_totals = {day: sum(faults.values()) for day, faults in day_fault.items()}
    top_day = max(day_totals.values(), default=0)
    per_day = [
        HealthDay(
            day=day,
            total=day_totals.get(day, 0),
            bar_percent=_bar_percent(day_totals.get(day, 0), top_day),
            faults=_fault_counts(day_fault.get(day, {})),
        )
        for day in (start_day + timedelta(days=offset) for offset in range(days))
    ]

    fixed = auto_fix_counts.get(HelpAutoFixState.FIXED, 0)
    not_fixed = auto_fix_counts.get(HelpAutoFixState.NOT_FIXED, 0)
    auto_fix = AutoFixStats(
        fixed=fixed,
        not_fixed=not_fixed,
        running=auto_fix_counts.get(HelpAutoFixState.RUNNING, 0),
        success_percent=(
            _percent(fixed, fixed + not_fixed) if fixed + not_fixed else None
        ),
    )

    ordered_resolutions = sorted(
        resolution_counts.items(),
        key=lambda item: (-item[1], _resolution_label(item[0])),
    )
    top_resolution = ordered_resolutions[0][1] if ordered_resolutions else 0
    resolutions = [
        ResolutionCount(
            resolution=resolution,
            label=_resolution_label(resolution),
            count=count,
            bar_percent=_bar_percent(count, top_resolution),
        )
        for resolution, count in ordered_resolutions
    ]

    return MachineHealth(
        days=days,
        start_day=start_day,
        end_day=end_day,
        total_requests=sum(fault_totals.values()),
        per_day=per_day,
        by_fault=_fault_counts(fault_totals),
        auto_fix=auto_fix,
        resolutions=resolutions,
    )
