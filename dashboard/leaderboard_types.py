"""Shared value types of the leaderboard page (rules, service, form, view)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

Medal = Literal["gold", "silver", "bronze", ""]


class LeaderboardPeriod(StrEnum):
    """The two views: since today's midnight, or the last 7 calendar days."""

    TODAY = "today"
    WEEK = "7d"

    @property
    def label(self) -> str:
        return "Today" if self is LeaderboardPeriod.TODAY else "Last 7 days"


DEFAULT_PERIOD = LeaderboardPeriod.WEEK


@dataclass(frozen=True)
class LeaderboardWindow:
    """``[start, end)`` and the equal window just before it ``[previous_start,
    previous_end)``; ``days`` is 1 or 7."""

    period: LeaderboardPeriod
    start: datetime
    end: datetime
    previous_start: datetime
    previous_end: datetime
    days: int
    time_zone_name: str


@dataclass(frozen=True)
class LeaderboardFilters:
    period: LeaderboardPeriod
    project_id: int | None
    help_mode: str | None


@dataclass(frozen=True)
class UserTally:
    """One user's raw numbers in one window."""

    passed: int = 0
    failed: int = 0
    blocked: int = 0
    needs_retest: int = 0
    steps_performed: int = 0
    help_resolved: int = 0

    @property
    def tests_done(self) -> int:
        return self.passed + self.failed + self.blocked + self.needs_retest


@dataclass(frozen=True)
class UserInput:
    """What the service gathers for one listed user; the rules turn a list of
    these into ranked ``LeaderboardRow`` s."""

    user_id: int
    name: str
    current: UserTally
    previous: UserTally
    need_help_now: int
    remaining: int
    # Local days of the current window with a counted verdict or a reported
    # person step (empty in the Today view).
    active_dates: frozenset[date]


@dataclass(frozen=True)
class Badge:
    key: str
    label: str
    title: str


@dataclass(frozen=True)
class LeaderboardRow:
    user_id: int
    name: str
    is_viewer: bool
    current: UserTally
    score: int
    previous_score: int
    rank: int
    previous_rank: int | None
    # Places gained since the previous window (positive is up); None when not
    # shown (no score now, or new).
    movement: int | None
    is_new: bool
    medal: Medal
    # Snapshots, not windowed.
    need_help_now: int
    remaining: int
    # 7-day view only (0 in the Today view).
    active_days: int
    streak: int
    badges: tuple[Badge, ...]


@dataclass(frozen=True)
class Leaderboard:
    window: LeaderboardWindow
    filters: LeaderboardFilters
    rows: list[LeaderboardRow]
    # At most 3 rows with ``score > 0``.
    podium: list[LeaderboardRow]
    viewer_row: LeaderboardRow | None
    # Counted results in the window credited to nobody.
    unattributed_count: int
    # ``(label, points)``, the single source of "How points work".
    points: tuple[tuple[str, int], ...]
