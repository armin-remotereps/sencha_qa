"""Leaderboard domain rules: the window, points, ranking, movement, streaks and
badges. Pure functions over ``leaderboard_types``; no ORM, no queries."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Final

from django.utils import timezone

from dashboard.leaderboard_types import (
    Badge,
    LeaderboardPeriod,
    LeaderboardRow,
    LeaderboardWindow,
    Medal,
    UserInput,
    UserTally,
)

PASSED_POINTS: Final = 2
FAILED_POINTS: Final = 2
BLOCKED_POINTS: Final = 1
NEEDS_RETEST_POINTS: Final = 0
STEP_PERFORMED_POINTS: Final = 1
HELP_RESOLVED_POINTS: Final = 1

# ``(label, points)`` in display order, the single source of "How points work".
POINTS: Final[tuple[tuple[str, int], ...]] = (
    ("Passed", PASSED_POINTS),
    ("Failed", FAILED_POINTS),
    ("Blocked", BLOCKED_POINTS),
    ("Needs re-test", NEEDS_RETEST_POINTS),
    ("Step performed", STEP_PERFORMED_POINTS),
    ("Help resolved", HELP_RESOLVED_POINTS),
)

PERIOD_DAYS: Final[dict[LeaderboardPeriod, int]] = {
    LeaderboardPeriod.TODAY: 1,
    LeaderboardPeriod.WEEK: 7,
}

MEDALS: Final[dict[int, Medal]] = {1: "gold", 2: "silver", 3: "bronze"}
PODIUM_SIZE: Final = 3

BUG_HUNTER: Final = Badge("bug_hunter", "Bug hunter", "Most failures found")
HANDS_ON: Final = Badge("hands_on", "Hands-on", "Most steps performed")
HELPER: Final = Badge("helper", "Helper", "Most help requests resolved")
PERFECT_WEEK: Final = Badge("perfect_week", "Perfect week", "Active on all 7 days")

# "Most X" badges, in display order, with the tally field they compare.
_MOST_BADGES: Final[tuple[tuple[Badge, Callable[[UserTally], int]], ...]] = (
    (BUG_HUNTER, lambda t: t.failed),
    (HANDS_ON, lambda t: t.steps_performed),
    (HELPER, lambda t: t.help_resolved),
)


def resolve_window(period: LeaderboardPeriod, now: datetime) -> LeaderboardWindow:
    """``[local midnight of the first day, now)`` and the same span ``days``
    calendar days earlier (built from local dates, so DST safe)."""
    days = PERIOD_DAYS[period]
    tz = timezone.get_current_timezone()
    local_now = timezone.localtime(now, tz)
    start_date = local_now.date() - timedelta(days=days - 1)
    previous_start_date = start_date - timedelta(days=days)
    previous_end = datetime.combine(
        local_now.date() - timedelta(days=days), local_now.time(), tzinfo=tz
    )
    return LeaderboardWindow(
        period=period,
        start=datetime.combine(start_date, time.min, tzinfo=tz),
        end=now,
        previous_start=datetime.combine(previous_start_date, time.min, tzinfo=tz),
        previous_end=previous_end,
        days=days,
        time_zone_name=timezone.get_current_timezone_name(),
    )


def score(tally: UserTally) -> int:
    """Points earned by one tally (see ``POINTS``)."""
    return (
        PASSED_POINTS * tally.passed
        + FAILED_POINTS * tally.failed
        + BLOCKED_POINTS * tally.blocked
        + NEEDS_RETEST_POINTS * tally.needs_retest
        + STEP_PERFORMED_POINTS * tally.steps_performed
        + HELP_RESOLVED_POINTS * tally.help_resolved
    )


def _competition_ranks(scores: dict[int, int]) -> dict[int, int]:
    """Standard competition rank (1, 1, 3) per user id, for scores above 0."""
    ordered = sorted((s for s in scores.values() if s > 0), reverse=True)
    first_place: dict[int, int] = {}
    for index, value in enumerate(ordered):
        first_place.setdefault(value, index + 1)
    return {uid: first_place[s] for uid, s in scores.items() if s > 0}


def _streak(active: frozenset[date], today: date, window_days: int) -> int:
    """Consecutive active days ending today, or yesterday when today has none
    yet; capped at ``window_days``."""
    day = today if today in active else today - timedelta(days=1)
    count = 0
    while count < window_days and day in active:
        count += 1
        day -= timedelta(days=1)
    return count


def _badges(
    users: Sequence[UserInput], window: LeaderboardWindow
) -> dict[int, tuple[Badge, ...]]:
    """Badges per user id; ties all earn one and 0 never does."""
    earned: dict[int, list[Badge]] = {u.user_id: [] for u in users}
    for badge, value in _MOST_BADGES:
        best = max((value(u.current) for u in users), default=0)
        if best <= 0:
            continue
        for u in users:
            if value(u.current) == best:
                earned[u.user_id].append(badge)
    if window.period is LeaderboardPeriod.WEEK:
        for u in users:
            if len(u.active_dates) == PERIOD_DAYS[LeaderboardPeriod.WEEK]:
                earned[u.user_id].append(PERFECT_WEEK)
    return {uid: tuple(badges) for uid, badges in earned.items()}


def build_rows(
    users: Sequence[UserInput], *, viewer_id: int, window: LeaderboardWindow
) -> list[LeaderboardRow]:
    """Ranked rows: score, then tests done, then name. Users with no score come
    last, sharing the rank after the scored ones, with no medal or movement."""
    current = {u.user_id: score(u.current) for u in users}
    previous = {u.user_id: score(u.previous) for u in users}
    ranks = _competition_ranks(current)
    previous_ranks = _competition_ranks(previous)
    unscored_rank = len(ranks) + 1
    badges = _badges(users, window)
    is_week = window.period is LeaderboardPeriod.WEEK
    today = timezone.localtime(window.end).date()

    ordered = sorted(
        users,
        key=lambda u: (
            -current[u.user_id],
            -u.current.tests_done,
            u.name.casefold(),
            u.user_id,
        ),
    )
    rows: list[LeaderboardRow] = []
    for u in ordered:
        now_score = current[u.user_id]
        rank = ranks.get(u.user_id, unscored_rank)
        previous_rank = previous_ranks.get(u.user_id)
        scored = now_score > 0
        rows.append(
            LeaderboardRow(
                user_id=u.user_id,
                name=u.name,
                is_viewer=u.user_id == viewer_id,
                current=u.current,
                score=now_score,
                previous_score=previous[u.user_id],
                rank=rank,
                previous_rank=previous_rank,
                movement=(
                    previous_rank - rank
                    if scored and previous_rank is not None
                    else None
                ),
                is_new=scored and previous[u.user_id] == 0,
                medal=MEDALS.get(rank, "") if scored else "",
                need_help_now=u.need_help_now,
                remaining=u.remaining,
                active_days=len(u.active_dates) if is_week else 0,
                streak=(_streak(u.active_dates, today, window.days) if is_week else 0),
                badges=badges[u.user_id],
            )
        )
    return rows


def podium(rows: Sequence[LeaderboardRow]) -> list[LeaderboardRow]:
    """The first rows (at most 3) that hold a medal."""
    return [row for row in rows if row.medal != ""][:PODIUM_SIZE]
